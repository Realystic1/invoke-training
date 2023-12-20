class Resolution:
    def __init__(self, height: int, width: int):
        self.height = height
        self.width = width

    def aspect_ratio(self):
        return self.height / self.width

    def __eq__(self, other: "Resolution") -> bool:
        return self.height == other.height and self.width == other.width

    def __hash__(self):
        return hash((self.height, self.width))


class AspectRatioBucketManager:
    def __init__(self, target_resolution: Resolution, start_dim: int, end_dim: int, divisible_by: int) -> None:
        self._buckets = list(
            self.build_aspect_ratio_buckets(
                target_resolution=target_resolution,
                start_dim=start_dim,
                end_dim=end_dim,
                divisible_by=divisible_by,
            )
        )

    @classmethod
    def build_aspect_ratio_buckets(
        cls, target_resolution: Resolution, start_dim: int, end_dim: int, divisible_by: int
    ) -> set[Resolution]:
        """Prepare a set of aspect ratios.

        Args:
            target_resolution (Resolution): The target resolution. All resolutions in the returned set will have <=
                the number of pixels in this target resolution.
            start_dim (int):
            end_dim (int):
            divisible_by (int): All dimensions in the returned set of resolutions will be divisible by `divisible_by`.

        Returns:
            set[tuple[int, int]]: The aspect ratio bucket resolutions.
        """
        # Validate target_resolution.
        assert target_resolution.height % divisible_by == 0
        assert target_resolution.width % divisible_by == 0

        # Validate start_dim, end_dim.
        assert start_dim <= end_dim
        assert start_dim % divisible_by == 0
        assert end_dim % divisible_by == 0

        target_size = target_resolution.height * target_resolution.width

        buckets = set()

        height = start_dim
        while height <= end_dim:
            width = (target_size // height) // divisible_by * divisible_by
            buckets.add(Resolution(height, width))
            buckets.add(Resolution(width, height))

            height += divisible_by

        return buckets

    def get_aspect_ratio_bucket(self, resolution: Resolution):
        """Get the bucket with the closest aspect ratio to 'resolution'."""
        # Note: If this is ever found to be a bottleneck, there is a clearly-more-efficient implementation using bisect.
        return min(self._buckets, key=lambda x: abs(x.aspect_ratio() - resolution.aspect_ratio()))
