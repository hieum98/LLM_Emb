import copy
from dataclasses import dataclass
import logging
from typing import Dict, List, Optional, Tuple, Union
import torch 
from transformers import (AutoConfig, 
                          MistralPreTrainedModel,
                          MistralForCausalLM,)
from transformers.utils import ModelOutput
from transformers.integrations import is_deepspeed_zero3_enabled, deepspeed_config
from pytorch_metric_learning import losses, miners, distances

from genc.model.modules import NextTokenLoss

logger = logging.getLogger(__name__)


@dataclass
class EmLMTrainOutput(ModelOutput):
    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    past_key_values: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[Tuple[torch.FloatTensor, ...]] = None
    loss_emb: Optional[torch.Tensor] = None
    reps: Optional[torch.Tensor] = None # [b, d]

class MistralEmbeddingLM(MistralForCausalLM):
    def __init__(
            self,
            config: AutoConfig,
            use_bidirectional: bool = False,
            normalized: bool = True,
            pooling_method: str = 'mean', # One of ['cls', 'lasttoken', 'mean', 'weightedmean']
            loss_gen_type: str = "mixed",
            temperature: float = 0.05,
            new_vocab_size: Optional[int] = None,
            ) -> None:
    
        super().__init__(config)
        # self.model = MistralForCausalLM(config)
        self.is_causal = not use_bidirectional
        self.normalized = normalized
        self.pooling_method = pooling_method

        # Embedding loss
        self.cons_loss = losses.NTXentLoss(
            temperature=temperature, 
            distance=distances.CosineSimilarity()
        )
        self.miner = miners.MultiSimilarityMiner(epsilon=0.2)

        # Generation loss
        vocab_size = new_vocab_size if new_vocab_size is not None else config.vocab_size
        self.gen_loss_fn = NextTokenLoss(vocab_size, loss_gen_type)
    
    def pooling(
        self, hidden_state: torch.Tensor, attention_mask: torch.Tensor = None, recast: bool = False
    ) -> torch.Tensor:
        """
        Args:
            hidden_state: [b, n, d]
            attention_mask: [b, n]
        """
        # In case the model is distributed across multiple devices; hidden_state may end up on diff device
        hidden_state = hidden_state.to(attention_mask.device)
        if self.pooling_method == 'cls':
            embedding = hidden_state[:, 0]
        elif self.pooling_method == 'lasttoken':
            b, n, d = hidden_state.size()
            # Get the last `1` in the attention mask of each item
            # Often it is just `gather_indices = torch.argmin(attention_mask, 1, keepdim=False) - 1`
            # except when 1) There's all 1's 2) There's 0's before the 1's
            reversed_mask = torch.flip(attention_mask, dims=(1,))
            argmax_reverse = torch.argmax(reversed_mask, dim=1, keepdim=False)
            gather_indices = attention_mask.size(1) - argmax_reverse - 1
            # If there are empty sequences, where the index would become -1 it will crash so set them to 0
            gather_indices = torch.clamp(gather_indices, min=0)
            # Turn indices from shape [b] -> [b, 1, d]
            gather_indices = gather_indices.unsqueeze(-1).repeat(1, d)
            gather_indices = gather_indices.unsqueeze(1)
            assert gather_indices.shape == (b, 1, d)
            # Gather along the seq len: [b, n, d] -> [b, d]
            # Actually no need for the attention mask as we gather the last token where attn_mask=1 but
            # as some indices (which shouldn't be attended to) may be 0 due to clamp, use mask to ignore them again
            input_mask_expanded = attention_mask.unsqueeze(-1).expand((b, n, d)).float()
            embedding = torch.gather(hidden_state * input_mask_expanded, 1, gather_indices).squeeze(dim=1)
        elif self.pooling_method in ['mean', 'weightedmean']:
            if self.pooling_method == 'weightedmean':
                attention_mask *= attention_mask.cumsum(dim=1) # [0,1,1,1,0,0] -> [0,1,2,3,0,0]
            s = torch.sum(hidden_state * attention_mask.unsqueeze(-1).float(), dim=1)
            d = attention_mask.sum(dim=1, keepdim=True).float()
            embedding = s / d
        else: raise NotImplementedError(f"Unknown pooling method: {self.pooling_method}")
        # Recasting performs slightly worse but saves 50% space
        if recast: return embedding.to(hidden_state.dtype)
        return embedding
    
    def encode(self,
               input_ids: torch.Tensor,
               attention_mask: torch.Tensor,
               prompt_length: Optional[torch.Tensor] = None,
               ) -> torch.Tensor:
        """
        Encode and pool the input sequence for embedding tasks.
        Args:
            input_ids: [b, n]
            attention_mask: [b, n]
            prompt_length: [b]
            is_causal: bool. You need to path the mistral modeling with modeling_mistral_em_lm.py in order to use this.
        Returns:
            hidden_state: [b, d]
        """
        kwargs = {'input_ids': input_ids, 
                  'attention_mask': attention_mask,}
        if self.is_causal:
            kwargs['is_causal'] = True
        
        # Get the hidden states
        outputs = self.model(**kwargs)[0] # [b, n, h]

        # Pool the hidden states
        # Mask the prompt tokens
        if prompt_length is not None:
            attention_mask = attention_mask.clone()
            for i, l in enumerate(prompt_length):
                attention_mask[i, :l] = 0
                # Make sure not all zeros - If this happens it is a bug
                assert attention_mask[i].sum() > 0, "You have all zeros in the attention mask!"
        reps = self.pooling(outputs, attention_mask)
        # Normalize the embeddings
        if self.normalized:
            in_dtype = reps.dtype
            # Normalize can change the dtype (https://discuss.pytorch.org/t/tensor-in-float16-is-transformed-into-float32-after-torch-norm/110891)
            return torch.nn.functional.normalize(reps, dim=-1).contiguous().to(in_dtype)
        
        return reps.contiguous()
    
    def cons_loss_fn(self, reps: torch.Tensor, constrastive_labels: torch.Tensor, use_miner: bool = False) -> torch.Tensor:
        """
        Calculate the constrastive loss.
        Args:
            reps: [b, d]
            constrastive_labels: [b]
            use_miner: bool
        """
        if use_miner:
            hard_pairs = self.miner(reps, constrastive_labels)
            loss = self.cons_loss(reps, constrastive_labels, hard_pairs)
        else:
            loss = self.cons_loss(reps, constrastive_labels)
        return loss
    
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        input_reps: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        constrastive_labels: Optional[torch.LongTensor] = None,
        loss_weight_mask: Optional[torch.Tensor] = None,
        prompt_length: Optional[torch.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = True,
        is_emb: bool = False,
        is_gen: bool = False,
        use_miner: bool = False,
        ) -> EmLMTrainOutput:
        """
        Args:
            input_ids: [b, n]
            attention_mask: [b, n]
            position_ids: [b, n]
            past_key_values: List[torch.FloatTensor]
            inputs_embeds: [b, n, d]
            labels: [b, n]
            constrastive_labels: [b]
            loss_weight_mask: [b, n]
            prompt_length: [b]
            use_cache: bool
            output_attentions: bool
            output_hidden_states: bool
            return_dict: bool
        """
        output = {
            "loss": None,
            "logits": None,
            "past_key_values": None,
            "hidden_states": None,
            "attentions": None,
            "loss_emb": None,
            "reps": None
        }

        if is_gen:
            gen_kwargs = {
                "return_dict": return_dict,
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "inputs_embeds": inputs_embeds,
                "use_cache": use_cache,
                "output_attentions": output_attentions,
                "output_hidden_states": output_hidden_states
            }
            gen_outputs = super().forward(
                input_ids=input_ids,
                attention_mask=attention_mask,
                **gen_kwargs
            )
            # Map all properties from the gen_outputs to the output
            for k, v in gen_outputs.items():
                output[k] = v
            if labels is not None:
                logits = gen_outputs.logits
                loss_gen = self.gen_loss_fn(labels, logits, loss_weight_mask)
                output['loss'] = loss_gen

        if is_emb:
            if input_reps is not None:
                reps = input_reps
            else:
                reps = self.encode(input_ids, attention_mask, prompt_length)
            output['reps'] = reps
            if constrastive_labels is not None:
                loss_emb = self.cons_loss_fn(reps, constrastive_labels, use_miner)
                output['loss_emb'] = loss_emb
        return output
        # return EmLMTrainOutput(**output)

    @classmethod
    def _from_config(cls, config, **kwargs):
        """
        All context managers that the model should be initialized under go here.

        Args:
            torch_dtype (`torch.dtype`, *optional*):
                Override the default `torch.dtype` and load the model under this dtype.
        """
        torch_dtype = kwargs.pop("torch_dtype", None)
        use_flash_attention_2 = kwargs.pop("use_flash_attention_2", False)

        # override default dtype if needed
        dtype_orig = None
        if torch_dtype is not None:
            dtype_orig = cls._set_default_torch_dtype(torch_dtype)

        config = copy.deepcopy(config)  # We do not want to modify the config inplace in _from_config.
        config._attn_implementation = kwargs.pop("attn_implementation", None)
        config = cls._autoset_attn_implementation(
            config,
            use_flash_attention_2=use_flash_attention_2,
            check_device_map=False,
            torch_dtype=torch_dtype,
        )

        if is_deepspeed_zero3_enabled():
            import deepspeed

            logger.info("Detected DeepSpeed ZeRO-3: activating zero.init() for this model")
            # this immediately partitions the model across all gpus, to avoid the overhead in time
            # and memory copying it on CPU or each GPU first
            with deepspeed.zero.Init(config_dict_or_path=deepspeed_config()):
                model = cls(config, **kwargs)
        else:
            model = cls(config, **kwargs)

        # restore default dtype if it was modified
        if dtype_orig is not None:
            torch.set_default_dtype(dtype_orig)

        return model


