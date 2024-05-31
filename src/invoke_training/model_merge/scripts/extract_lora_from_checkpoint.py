# This script is based on
# https://raw.githubusercontent.com/kohya-ss/sd-scripts/bfb352bc433326a77aca3124248331eb60c49e8c/networks/extract_lora_from_models.py
# That script was originally based on https://github.com/cloneofsimo/lora/blob/develop/lora_diffusion/cli_svd.py


import argparse
import logging
import sys
from pathlib import Path
from typing import Literal

import peft
import torch
from diffusers import UNet2DConditionModel

from invoke_training._shared.accelerator.accelerator_utils import get_dtype_from_str
from invoke_training._shared.stable_diffusion.lora_checkpoint_utils import (
    UNET_TARGET_MODULES,
    save_sdxl_kohya_checkpoint,
)
from invoke_training.model_merge.extract_lora import (
    PEFT_BASE_LAYER_PREFIX,
    extract_lora_from_diffs,
    get_patched_base_weights_from_peft_model,
    get_state_dict_diff,
)


def str_to_device(device_str: Literal["cuda", "cpu"]) -> torch.device:
    if device_str == "cuda":
        return torch.device("cuda")
    elif device_str == "cpu":
        return torch.device("cpu")
    else:
        raise ValueError(f"Unexpected device: {device_str}")


def load_sdxl_unet(model_path: str) -> UNet2DConditionModel:
    variants_to_try = [None, "fp16"]
    unet = None
    for variant in variants_to_try:
        try:
            unet = UNet2DConditionModel.from_pretrained(model_path, variant=variant, local_files_only=True)
        except OSError as e:
            if "no file named" in str(e):
                # Ok. We'll try a different variant.
                pass
            else:
                raise
    if unet is None:
        raise RuntimeError(f"Failed to load UNet from '{model_path}'.")
    return unet


def state_dict_to_device(state_dict: dict[str, torch.Tensor], device: torch.device) -> dict[str, torch.Tensor]:
    return {k: v.to(device=device) for k, v in state_dict.items()}


@torch.no_grad()
def extract_lora(
    logger: logging.Logger,
    model_type: Literal["sd1", "sdxl"],
    model_orig_path: str,
    model_tuned_path: str,
    save_to: str,
    load_precision: Literal["float32", "float16", "bfloat16"],
    save_precision: Literal["float32", "float16", "bfloat16"],
    device: Literal["cuda", "cpu"],
    lora_rank: int,
    clamp_quantile=0.99,
):
    load_dtype = get_dtype_from_str(load_precision)
    save_dtype = get_dtype_from_str(save_precision)
    device = str_to_device(device)

    # Load models.
    if model_type == "sd1":
        raise NotImplementedError("SD1 support is not yet implemented.")
    elif model_type == "sdxl":
        logger.info(f"Loading original SDXL model: '{model_orig_path}'.")
        unet_orig = load_sdxl_unet(model_orig_path)
        logger.info(f"Loading tuned SDXL model: '{model_tuned_path}'.")
        unet_tuned = load_sdxl_unet(model_tuned_path)

        if load_dtype is not None:
            unet_orig = unet_orig.to(load_dtype)
            unet_tuned = unet_tuned.to(load_dtype)
    else:
        raise ValueError(f"Unexpected model type: '{model_type}'.")

    # Apply LoRA to the UNet.
    # The only reason we do this is to get the module names for the weights that we'll extract. We don't actually use
    # the LoRA weights initialized here.
    unet_lora_config = peft.LoraConfig(
        r=lora_rank,
        # We set the alpha to the rank, because we don't want any scaling to be applied to the LoRA weights that we
        # extract.
        lora_alpha=lora_rank,
        target_modules=UNET_TARGET_MODULES,
    )
    unet_tuned = peft.get_peft_model(unet_tuned, unet_lora_config)
    unet_orig = peft.get_peft_model(unet_orig, unet_lora_config)

    unet_tuned_base_weights = get_patched_base_weights_from_peft_model(unet_tuned)
    unet_orig_base_weights = get_patched_base_weights_from_peft_model(unet_orig)

    diffs = get_state_dict_diff(unet_tuned_base_weights, unet_orig_base_weights)

    # Clear tuned UNet to save memory.
    # TODO(ryand): We also need to clear the state_dicts. Move the diff extraction to a separate function so that memory
    # cleanup is handled by scoping.
    del unet_tuned

    # Apply SVD (Singluar Value Decomposition) to the diffs.
    # We just use the device for this calculation, since it's slow, then we move the results back to the CPU.
    logger.info("Calculating LoRA weights with SVD.")
    diffs = state_dict_to_device(diffs, device)
    lora_weights = extract_lora_from_diffs(
        diffs=diffs, rank=lora_rank, clamp_quantile=clamp_quantile, out_dtype=save_dtype
    )

    # Prepare state dict for LoRA.
    lora_state_dict = {}
    for module_name, (lora_up, lora_down) in lora_weights.items():
        lora_state_dict[PEFT_BASE_LAYER_PREFIX + module_name + ".lora_A.default.weight"] = lora_down
        lora_state_dict[PEFT_BASE_LAYER_PREFIX + module_name + ".lora_B.default.weight"] = lora_up
        # TODO(ryand): Double-check that this isn't needed with peft.
        # lora_state_dict[peft_base_layer_suffix + module_name + ".alpha"] = torch.tensor(down_weight.size()[0])

    lora_state_dict = state_dict_to_device(lora_state_dict, torch.device("cpu"))

    # Load the state_dict into the LoRA model.
    unet_orig.load_state_dict(lora_state_dict, strict=False, assign=True)

    save_to_path = Path(save_to)
    assert save_to_path.suffix == ".safetensors"
    if save_to_path.exists():
        raise FileExistsError(f"Destination file already exists: '{save_to}'.")
    save_to_path.parent.mkdir(parents=True, exist_ok=True)
    save_sdxl_kohya_checkpoint(save_to_path, unet=unet_orig, text_encoder_1=None, text_encoder_2=None)

    logger.info(f"Saved LoRA weights to: {save_to_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-type", type=str, required=True, choices=["sd1", "sdxl"], help="The base model type.")

    parser.add_argument(
        "--model-orig",
        type=str,
        required=True,
        help="Path to the original model. (This should be a unet directory in diffusers format.)",
    )
    parser.add_argument(
        "--model-tuned",
        type=str,
        required=True,
        help="Path to the tuned model. (This should be a unet directory in diffusers format.)",
    )
    parser.add_argument(
        "--save-to",
        type=str,
        required=True,
        help="Destination file path (must have a .safetensors extension).",
    )
    parser.add_argument(
        "--load-precision",
        type=str,
        default="bfloat16",
        choices=["float32", "float16", "bfloat16"],
        help="Model load precision.",
    )
    parser.add_argument(
        "--save-precision",
        type=str,
        default="float16",
        choices=["float32", "float16", "bfloat16"],
        help="Model save precision.",
    )

    parser.add_argument("--lora-rank", type=int, default=4, help="LoRA rank dimension.")
    parser.add_argument("--clamp-quantile", type=float, default=0.99, help="Quantile clamping value. (0-1)")
    parser.add_argument(
        "--device", type=str, default="cuda", choices=["cuda", "cpu"], help="Device to use. (cuda or cpu)"
    )

    args = parser.parse_args()

    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    extract_lora(
        logger=logger,
        model_type=args.model_type,
        model_orig_path=args.model_orig,
        model_tuned_path=args.model_tuned,
        save_to=args.save_to,
        load_precision=args.load_precision,
        save_precision=args.save_precision,
        device=args.device,
        lora_rank=args.lora_rank,
        clamp_quantile=args.clamp_quantile,
    )


if __name__ == "__main__":
    main()
