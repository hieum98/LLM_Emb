#!/bin/bash -l

# SLURM SUBMIT SCRIPT
#SBATCH --nodes=8              # This needs to match Fabric(num_nodes=...)
#SBATCH --ntasks-per-node=1    # This needs to match Fabric(devices=...)
#SBATCH --gres=gpu:1           # Request N GPUs per machine
#SBATCH --mem=200G       
#SBATCH --constraint=gpu-40gb|gpu-80gb|h100
#SBATCH --cpus-per-task=20
#SBATCH --job-name=genclm
#SBATCH --partition=gpulong
#SBATCH --account=uonlp
#SBATCH --output=/home/hieum/uonlp/LLM_Emb/genclm-7b-msmarco-%j.out
#SBATCH --error=/home/hieum/uonlp/LLM_Emb/genclm-7b-msmarco-%j.err

# Activate conda environment
source activate llm
cd /home/hieum/uonlp/LLM_Emb

# Debugging flags (optional)
# export NCCL_DEBUG=INFO
# export PYTHONFAULTHANDLER=1

# On your cluster you might need this:
# export NCCL_SOCKET_IFNAME=^docker0,lo

# Run your training script
srun python -m genc.main --config_file scripts/configs/msmarco.yaml --nodes 9 --devices 1 --mode esft --output_dir output/genclm_esft_msmarco_7b

