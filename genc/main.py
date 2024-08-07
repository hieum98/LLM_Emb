import logging
from math import sqrt
import os
from pathlib import Path
import yaml
from dataclasses import asdict
import torch
from torch.distributed.fsdp import MixedPrecision
from torch.distributed.fsdp.api import CPUOffload, ShardingStrategy
import lightning as L
from lightning import seed_everything
from lightning.fabric.strategies import FSDPStrategy, DDPStrategy
from transformers import get_cosine_schedule_with_warmup, PreTrainedTokenizerBase, HfArgumentParser

from genc.data.base import DataModule
from genc.data.genclm_data import GenCLMDataset
from genc.data.msmarco import MSMARCODataset
from genc.model.modeling_lamma_genc_lm import LlamaDecoderLayer
from genc.model.modeling_mistral_genc_lm import MistralDecoderLayer
from genc.model.modeling_phi_genc_lm import PhiDecoderLayer
from genc.trainer.gradcache_trainer import fit as genclm_fit
from genc.trainer.sft_trainer import fit as sft_fit
from genc.trainer.trainer_utils import (
    choose_logger,
    get_default_supported_precision,
    get_wrapping_policy,
    lora_filter,
)
from genc.trainer.load_model import load_model
from genc.args import DataArguments, ModelArguments, TrainingArguments, ValidationArgument


def validate_and_correct_args(
        data_args: DataArguments,
        model_args: ModelArguments,
        training_args: TrainingArguments,
        validation_args: ValidationArgument,
        ):
    # Validate precision
    precision = training_args.precision or get_default_supported_precision(training=True)
    training_args.precision = precision

    if 'bf16' in training_args.precision and not torch.cuda.is_bf16_supported():
        raise ValueError('Current device does not support bfloat16')
    
    # Set no_sync if using cpu_offload and gradient accumulation. Turn off if not using gradient accumulation
    gradient_accumulation_iters = training_args.batch_size(training_args.devices) // training_args.mini_batch_size
    assert gradient_accumulation_iters > 0, "Batch size must be divisible by mini batch size"
    assert training_args.batch_size(training_args.devices) % training_args.mini_batch_size == 0, "Batch size must be divisible by mini batch size"
    if training_args.use_cpu_offload and gradient_accumulation_iters > 1:
        training_args.no_sync = True
    elif training_args.no_sync and gradient_accumulation_iters == 1:
        training_args.no_sync = False

    if model_args.gen_adapter_name is None and model_args.emb_adapter_name is not None:
        print("No adapter name for gen is provided. Using the same name for both embedding and generator adapters")
        model_args.gen_adapter_name = model_args.emb_adapter_name
    if model_args.emb_adapter_name is None and model_args.gen_adapter_name is None:
        print("No adapter name is provided. Using the default adapter name")
        model_args.emb_adapter_name = "default"
        model_args.gen_adapter_name = "default"

    # Save the corrected args into the yaml file
    config_file = Path(training_args.output_dir) / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w") as f:
        yaml.dump(asdict(training_args), f)
        yaml.dump(asdict(model_args), f)
        yaml.dump(asdict(data_args), f)
        yaml.dump(asdict(validation_args), f)
        
    return data_args, model_args, training_args, validation_args 

def get_dataloaders(
    fabric: L.Fabric,
    data: DataModule,
    tokenizer: PreTrainedTokenizerBase,
    training_args: TrainingArguments,
    pretrained_type: str,       
):  
    data.connect(
        world_size=fabric.world_size,
        global_rank=fabric.global_rank,
        tokenizer=tokenizer, 
        batch_size=training_args.batch_size(fabric.world_size), 
        global_batch_size=training_args.global_batch_size,
        max_seq_length=training_args.max_seq_length,
        num_negative_samples=training_args.num_negative_samples,
        num_positive_samples=training_args.num_positive_samples,
        prompt_loss_weight=training_args.prompt_loss_weight,
        pretrained_type=pretrained_type,
    )
    with fabric.rank_zero_first():
        data.prepare_data()
    data.setup()
    train_dataloader = data.train_dataloader()
    val_dataloader = data.val_dataloader()
    train_dataloader, val_dataloader = fabric.setup_dataloaders(
        train_dataloader, 
        val_dataloader, 
        use_distributed_sampler=False,
        move_to_device=True,
        )
    return train_dataloader, val_dataloader

def main(
    fabric: L.Fabric,
    data: DataModule,
    model_args: ModelArguments,
    training_args: TrainingArguments,
    validation_args: ValidationArgument,
    torch_dtype: torch.dtype,
    compute_dtype: torch.dtype,
):    
    fabric.seed_everything(training_args.seed)
    # Load the model and tokenizer
    model, tokenizer = load_model(
        model_weights_name_or_path=model_args.model_name_or_path,
        pretrained_type=model_args.pretrained_type,
        use_bidirectional=model_args.use_bidirectional,
        normalized=model_args.normalized,
        pooling_method=model_args.pooling_method,
        loss_gen_type=model_args.loss_gen_type,
        temperature=model_args.temperature,
        quantization=model_args.quantization,
        use_lora=model_args.use_lora,
        emb_adapter_name=model_args.emb_adapter_name,
        gen_adapter_name=model_args.gen_adapter_name,
        lora_target_modules=["all"],
        lora_r=model_args.lora_r,
        lora_alpha=model_args.lora_alpha,
        lora_dropout=model_args.lora_dropout,
        inference=False,
        low_memory=training_args.low_memory,
        torch_dtype=torch_dtype,
        compute_dtype=compute_dtype,
        precision=training_args.precision,
        rank=fabric.global_rank,
        local_rank=fabric.local_rank,
        gradient_checkpointing=training_args.gradient_checkpointing,
        attn_implementation=model_args.attn_implementation,
    )
    fabric.print("Model architecture")
    fabric.print(model)
    
    ref_model = None
    if training_args.mode in ['edpo', 'ecpo']:
        if model_args.ref_model_name_or_path is not None:
            ref_model, _ = load_model(
                model_weights_name_or_path=model_args.ref_model_name_or_path,
                pretrained_type=model_args.pretrained_type,
                use_bidirectional=model_args.use_bidirectional,
                normalized=model_args.normalized,
                pooling_method=model_args.pooling_method,
                loss_gen_type=model_args.loss_gen_type,
                temperature=model_args.temperature,
                quantization=True,
                use_lora=False,
                inference=True,
                low_memory=training_args.low_memory,
                torch_dtype=torch_dtype,
                compute_dtype=compute_dtype,
                precision=training_args.precision,
                rank=fabric.global_rank,
                local_rank=fabric.local_rank,
                attn_implementation=model_args.attn_implementation,
            )
        else:
            fabric.print("No reference model is provided. Using the same model for reference.")

    model = fabric.setup_module(model)
    fabric.print("Model architecture after wrapping")
    fabric.print(model)

    # Load the data
    train_dataloader, val_dataloader = get_dataloaders(fabric, data, tokenizer, training_args, model_args.pretrained_type)
    # Synchronize at the start
    fabric.barrier()

    steps_per_epoch = len(train_dataloader)
    lr_max_steps = min(
        training_args.num_train_epochs * steps_per_epoch, 
        (training_args.max_steps or float("inf"))
        )
    warmup_steps = 0.1 * lr_max_steps
    fabric.print(f"Total number of training steps: {lr_max_steps}")
    # Config optimizer and scheduler
    lr = training_args.learning_rate
    optimizer = torch.optim.AdamW(
        model.parameters(), 
        lr=lr, 
        weight_decay=training_args.weight_decay,
        betas=(training_args.adam_beta1, training_args.adam_beta2),
        )
    optimizer = fabric.setup_optimizers(optimizer)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer,
        num_warmup_steps=warmup_steps,
        num_training_steps=lr_max_steps,
        )
    checkpoint_iter_num = 0
    stage = {
        "iter_num": checkpoint_iter_num,
        "optimizer": optimizer,
        "scheduler": scheduler,
        "model": model,
    }
    if training_args.checkpoint_path is not None:
        optim_checkpoint_path = Path(training_args.checkpoint_path)
        if optim_checkpoint_path.exists():
            fabric.load(optim_checkpoint_path, stage, strict=False)

    model = stage.pop("model")
    if training_args.mode == 'sft':
        sft_fit(
            fabric=fabric,
            model=model,
            stage=stage,
            train_dataloader=train_dataloader,
            val_dataloader=val_dataloader,
            model_args=model_args,
            training_args=training_args,
            validation_args=validation_args,
            )
    else:
        genclm_fit(
            fabric=fabric,
            model=model,
            ref_model=ref_model,
            stage=stage,
            train_dataloader=train_dataloader,
            val_dataloader=val_dataloader,
            model_args=model_args,
            training_args=training_args,
            validation_args=validation_args,
            )

    torch.cuda.synchronize()
    save_full_path = Path(training_args.output_dir)/ training_args.mode / "final" / "model.ckpt"
    save_full_path.parent.mkdir(parents=True, exist_ok=True)
    print("Saving full model weights to", save_full_path)
    fabric.save(save_full_path, {'model':model}, filter={"model": lora_filter})
    fabric.barrier()
    # Clean up the gpu memory
    del model
    torch.cuda.empty_cache()
    # Reload the model from final checkpoint, merge the adapters and save only do on rank 0
    if fabric.global_rank == 0:
        model, tokenizer = load_model(
            model_weights_name_or_path=model_args.model_name_or_path,
            pretrained_type=model_args.pretrained_type,
            use_bidirectional=model_args.use_bidirectional,
            normalized=model_args.normalized,
            pooling_method=model_args.pooling_method,
            loss_gen_type=model_args.loss_gen_type,
            temperature=model_args.temperature,
            quantization=model_args.quantization,
            use_lora=model_args.use_lora,
            emb_adapter_name=model_args.emb_adapter_name,
            gen_adapter_name=model_args.gen_adapter_name,
            lora_target_modules=["all"],
            lora_r=model_args.lora_r,
            lora_alpha=model_args.lora_alpha,
            lora_dropout=model_args.lora_dropout,
            inference=False,
            low_memory=training_args.low_memory,
            torch_dtype=torch_dtype,
            compute_dtype=compute_dtype,
            precision=training_args.precision,
            rank=fabric.global_rank,
            local_rank=fabric.local_rank,
            gradient_checkpointing=training_args.gradient_checkpointing,
            attn_implementation=model_args.attn_implementation,
        )
        checkpoint = torch.load(save_full_path, map_location='cpu')
        model.load_state_dict(checkpoint['model'], strict=False)
        to_merge_adapter = model_args.emb_adapter_name if training_args.mode != 'sft' else model_args.gen_adapter_name
        model = model.merge_and_unload(adapter_names=[to_merge_adapter], progressbar=True)
        save_dir = Path(training_args.output_dir)
        save_dir = save_dir / save_dir.name
        model.save_pretrained(save_dir)
        tokenizer.save_pretrained(save_dir)
        fabric.print(f"Model and tokenizer are saved to {save_dir}")

def setup(
    data_args: DataArguments,
    model_args: ModelArguments,
    training_args: TrainingArguments,
    validation_args: ValidationArgument,
):
    data_args, model_args, training_args, validation_args = validate_and_correct_args(
        data_args=data_args,
        model_args=model_args,
        training_args=training_args,
        validation_args=validation_args,
    )
    seed_everything(training_args.seed)

    if data_args.data_name == 'msmarco':
        data = MSMARCODataset(
            data_dir=data_args.data_dir,
            train_file=data_args.train_file,
            val_file=data_args.val_file,
            ignore_index=data_args.ignore_index,
            seed=training_args.seed,
            num_workers=data_args.num_workers,
        )
    elif data_args.data_name == 'genclm':
        data = GenCLMDataset(
            data_dir=data_args.data_dir,
            val_file=data_args.val_file,
            seed=training_args.seed,
            num_workers=data_args.num_workers,
            ignore_index=data_args.ignore_index,
            max_data_samples=data_args.max_data_samples,
        )
    else:
        raise ValueError(f"We currently have not supported this dataset {data_args.data_name}")

    # Make necessary directories
    out_dir = Path(training_args.output_dir)
    os.makedirs(out_dir, exist_ok=True)

    mp_policy = None
    if training_args.precision == "bf16-true":
        torch_dtype, compute_dtype = torch.bfloat16, torch.bfloat16
    elif training_args.precision == "32":
        torch_dtype, compute_dtype = torch.float32, torch.float16
    elif training_args.precision == "16-mixed":
        compute_dtype, torch_dtype = torch.float16, torch.float32
        mp_policy = MixedPrecision(param_dtype=torch.float32, reduce_dtype=torch.float32, buffer_dtype=torch.float32)
    elif training_args.precision == "bf16-mixed":
        compute_dtype, torch_dtype = torch.bfloat16, torch.float32
        mp_policy = MixedPrecision(param_dtype=torch.float32, reduce_dtype=torch.float32, buffer_dtype=torch.float32)
    else:
        raise ValueError("Invalid precision")
    
    strategy = training_args.strategy
    if training_args.nodes > 1 or training_args.devices > 1:
        if training_args.strategy == 'fsdp':
            cpu_offload=CPUOffload(offload_params=True) if training_args.use_cpu_offload else False,
            # Config sharding strategy
            if training_args.sharding_strategy == "full_shard":
                sharding_strategy = ShardingStrategy.FULL_SHARD
            elif training_args.sharding_strategy == "shard_grad_op":
                sharding_strategy = ShardingStrategy.SHARD_GRAD_OP
            elif training_args.sharding_strategy == "ddp":
                sharding_strategy = ShardingStrategy.NO_SHARD
            elif training_args.sharding_strategy == "hybrid_full_shard":
                sharding_strategy = ShardingStrategy.HYBRID_SHARD
            elif training_args.sharding_strategy == "hybrid_shard_grad_op":
                sharding_strategy = ShardingStrategy._HYBRID_SHARD_ZERO2
            else:
                raise ValueError("Invalid sharding strategy")
            # Config auto wrap policy
            if model_args.pretrained_type == 'phi':
                wrapping_policy = get_wrapping_policy(PhiDecoderLayer)
            elif model_args.pretrained_type == 'mistral':
                wrapping_policy = get_wrapping_policy(MistralDecoderLayer)
            elif model_args.pretrained_type == 'llama':
                wrapping_policy = get_wrapping_policy(LlamaDecoderLayer)
                
            strategy = FSDPStrategy(
                cpu_offload=cpu_offload,
                mixed_precision=mp_policy,
                auto_wrap_policy=wrapping_policy,
                sharding_strategy=sharding_strategy,
                limit_all_gathers=True, # See https://github.com/pytorch/pytorch/issues/91165
                state_dict_type="full",
                sync_module_states=True,
            )
    else:
        strategy = "auto"
    
    logger_dir = Path(training_args.output_dir) / f"logs_{training_args.logger_name}"
    logger_name = f"genclm-{model_args.model_name_or_path.split('/')[-1]}"
    logger = choose_logger(training_args.logger_name, logger_dir, name=logger_name, log_interval=training_args.log_interval)

    fabric = L.Fabric(
        accelerator='gpu',
        strategy=strategy,
        devices=training_args.devices,
        num_nodes=training_args.nodes,
        precision=training_args.precision,
        loggers=logger,
    )
    fabric.launch(
        main,
        data=data,
        model_args=model_args,
        training_args=training_args,
        validation_args=validation_args,
        torch_dtype=torch_dtype,
        compute_dtype=compute_dtype,
    )


if __name__=='__main__':
    os.environ['TRANSFORMERS_NO_ADVISORY_WARNINGS'] = 'true'
    # os.environ['HF_HOME'] = '/mnt/hieu/hf_cache'
    # os.environ['TRANSFORMERS_CACHE'] = '/mnt/hieu/hf_cache'
    torch.set_float32_matmul_precision("high")

    import argparse
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--config_file", type=str, required=True, help="Path to the yaml config file",
    )
    parser.add_argument(
        "--nodes", type=int, default=1, help="Number of nodes"
    )
    parser.add_argument(
        "--devices", type=int, default=1, help="Number of devices"
    )
    parser.add_argument(
        "--mode", type=str, default="esft", help="Training mode"
    )
    parser.add_argument(
        "--ref_model_name_or_path", type=str, default=None, help="Reference model name or path"
    )
    parser.add_argument(
        "--output_dir", type=str, default="output", help="Output directory"
    )
    parser.add_argument(
        "--checkpoint_path", type=str, default=None, help="Checkpoint path to resume training"
    )

    args = parser.parse_args()
    config_file = args.config_file

    hf_parser = HfArgumentParser((DataArguments, ModelArguments, TrainingArguments, ValidationArgument))
    logging.info(f"Loading yaml config {config_file}")
    data_args, model_args, training_args, validation_args = hf_parser.parse_yaml_file(yaml_file=config_file)
    # Add read-only arguments
    training_args.nodes = args.nodes
    training_args.devices = args.devices
    training_args.mode = args.mode
    model_args.ref_model_name_or_path = args.ref_model_name_or_path
    training_args.output_dir = args.output_dir
    training_args.checkpoint_path = args.checkpoint_path
    
    setup(
        data_args=data_args,
        model_args=model_args,
        training_args=training_args,
        validation_args=validation_args,
    )



