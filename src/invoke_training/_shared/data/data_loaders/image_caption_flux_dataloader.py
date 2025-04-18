import typing

import torch
from torch.utils.data import DataLoader

from invoke_training._shared.data.data_loaders.image_caption_sd_dataloader import (
    build_aspect_ratio_bucket_manager,
    sd_image_caption_collate_fn as flux_image_caption_collate_fn,
)
from invoke_training._shared.data.datasets.build_dataset import (
    build_hf_hub_image_caption_dataset,
    build_image_caption_dir_dataset,
    build_image_caption_jsonl_dataset,
)
from invoke_training._shared.data.datasets.transform_dataset import TransformDataset
from invoke_training._shared.data.samplers.aspect_ratio_bucket_batch_sampler import (
    AspectRatioBucketBatchSampler,
)
from invoke_training._shared.data.transforms.caption_prefix_transform import CaptionPrefixTransform
from invoke_training._shared.data.transforms.drop_field_transform import DropFieldTransform
from invoke_training._shared.data.transforms.flux_image_transform import FluxImageTransform
from invoke_training._shared.data.transforms.load_cache_transform import LoadCacheTransform
from invoke_training._shared.data.transforms.tensor_disk_cache import TensorDiskCache
from invoke_training._shared.data.utils.aspect_ratio_bucket_manager import AspectRatioBucketManager
from invoke_training.config.data.data_loader_config import AspectRatioBucketConfig, ImageCaptionFluxDataLoaderConfig
from invoke_training.config.data.dataset_config import (
    HFHubImageCaptionDatasetConfig,
    ImageCaptionDirDatasetConfig,
    ImageCaptionJsonlDatasetConfig,
)


def build_image_caption_flux_dataloader(  # noqa: C901
    config: ImageCaptionFluxDataLoaderConfig,
    batch_size: int,
    use_masks: bool = False,
    text_encoder_output_cache_dir: typing.Optional[str] = None,
    text_encoder_cache_field_to_output_field: typing.Optional[dict[str, str]] = None,
    vae_output_cache_dir: typing.Optional[str] = None,
    shuffle: bool = True,
) -> DataLoader:
    """Construct a DataLoader for an image-caption dataset for Flux.1-dev.

    Args:
        config (ImageCaptionFluxDataLoaderConfig): The dataset config.
        batch_size (int): The DataLoader batch size.
        text_encoder_output_cache_dir (str, optional): The directory where text encoder outputs are cached and should be
            loaded from. If set, then the TokenizeTransform will not be applied.
        vae_output_cache_dir (str, optional): The directory where VAE outputs are cached and should be loaded from. If
            set, then the image augmentation transforms will be skipped, and the image will not be copied to VRAM.
        shuffle (bool, optional): Whether to shuffle the dataset order.
    Returns:
        DataLoader
    """
    if isinstance(config.dataset, HFHubImageCaptionDatasetConfig):
        base_dataset = build_hf_hub_image_caption_dataset(config.dataset)
    elif isinstance(config.dataset, ImageCaptionJsonlDatasetConfig):
        base_dataset = build_image_caption_jsonl_dataset(config.dataset)
    elif isinstance(config.dataset, ImageCaptionDirDatasetConfig):
        base_dataset = build_image_caption_dir_dataset(config.dataset)
    else:
        raise ValueError(f"Unexpected dataset config type: '{type(config.dataset)}'.")

    # Initialize either the fixed target resolution or aspect ratio buckets.
    if config.aspect_ratio_buckets is None:
        target_resolution = config.resolution
        aspect_ratio_bucket_manager = None
        batch_sampler = None
    else:
        target_resolution = None
        aspect_ratio_bucket_manager = build_aspect_ratio_bucket_manager(config=config.aspect_ratio_buckets)
        # TODO(ryand): Drill-down the seed parameter rather than hard-coding to 0 here.
        batch_sampler = AspectRatioBucketBatchSampler.from_image_sizes(
            bucket_manager=aspect_ratio_bucket_manager,
            image_sizes=base_dataset.get_image_dimensions(),
            batch_size=batch_size,
            shuffle=shuffle,
            seed=0,
        )

    all_transforms = []

    if config.caption_prefix is not None:
        all_transforms.append(CaptionPrefixTransform(caption_field_name="caption", prefix=config.caption_prefix + " "))

    if vae_output_cache_dir is None:
        image_field_names = ["image"]
        if use_masks:
            image_field_names.append("mask")
        else:
            all_transforms.append(DropFieldTransform("mask"))

        all_transforms.append(
            FluxImageTransform(
                image_field_names=image_field_names,
                fields_to_normalize_to_range_minus_one_to_one=["image"],
                resolution=config.resolution,
                aspect_ratio_bucket_manager=aspect_ratio_bucket_manager,
                random_flip=config.random_flip,
            )
        )
    else:
        # We drop the image to avoid having to either convert from PIL, or handle PIL batch collation.
        all_transforms.append(DropFieldTransform("image"))
        all_transforms.append(DropFieldTransform("mask"))

        vae_cache = TensorDiskCache(vae_output_cache_dir)

        cache_field_to_output_field = {
            "vae_output": "vae_output",
            "original_size_hw": "original_size_hw",
            "crop_top_left_yx": "crop_top_left_yx",
        }
        if use_masks:
            cache_field_to_output_field["mask"] = "mask"
        all_transforms.append(
            LoadCacheTransform(
                cache=vae_cache,
                cache_key_field="id",
                cache_field_to_output_field=cache_field_to_output_field,
            )
        )

    if text_encoder_output_cache_dir is not None:
        assert text_encoder_cache_field_to_output_field is not None
        text_encoder_cache = TensorDiskCache(text_encoder_output_cache_dir)
        all_transforms.append(
            LoadCacheTransform(
                cache=text_encoder_cache,
                cache_key_field="id",
                cache_field_to_output_field=text_encoder_cache_field_to_output_field,
            )
        )
    dataset = TransformDataset(base_dataset, all_transforms)

    if batch_sampler is None:
        return DataLoader(
            dataset,
            shuffle=shuffle,
            collate_fn=flux_image_caption_collate_fn,
            batch_size=batch_size,
            num_workers=config.dataloader_num_workers,
        )
    else:
        return DataLoader(
            dataset,
            batch_sampler=batch_sampler,
            collate_fn=flux_image_caption_collate_fn,
            num_workers=config.dataloader_num_workers,
        )
