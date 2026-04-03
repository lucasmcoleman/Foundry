# Open Questions

Items that could not be automatically resolved during the production hardening pass.

## Architecture

1. **UI authentication**: The FastAPI UI serves on `0.0.0.0:7865` with no authentication. For production deployment, this needs an auth layer (API key, OAuth, or at minimum IP allowlisting). Currently assumed to be used on trusted local networks only.

2. **MagicQuant integration method**: Foundry currently imports MagicQuant via a symlink and via an installed package in the venv. The correct long-term solution is to declare `magicquant` as a dependency in `pyproject.toml` and install it (either from PyPI, git, or local path). The symlink approach works but is fragile. The `pyproject.toml` declares `magicquant>=0.1.0` but this requires the package to be `pip install`-able.

3. **GPU memory management**: The pipeline does not have explicit GPU memory checking before starting stages. If another process (e.g., LM Studio) is using GPU memory, training will OOM without a clear error. A pre-flight GPU memory check would improve UX.

## Data

4. **Training data in the repo**: `data/zeroclaw_training_data.jsonl` is tracked in git. For production, training data should be stored separately (HuggingFace Datasets, S3, etc.) and not committed to the repo. This is noted but not resolved as it would require changing the training workflow.

## Dependencies

5. **ROCm-specific packages**: The unsloth-env contains ROCm-specific torch builds (`torch==2.11.0a0+rocm7.11.0a20260106`). The `pyproject.toml` declares `torch>=2.0.0` without specifying the ROCm variant. Users on AMD will need to install the ROCm torch variant manually per the PyTorch installation instructions. This is standard practice but should be documented.

6. **unsloth package**: The unsloth-env contains `unsloth==2026.3.4` and `unsloth_zoo==2026.3.2`, but the pipeline has migrated away from Unsloth to custom fast loaders. These packages are NOT needed. Confirmed safe to exclude from `pyproject.toml`.
