import hashlib
import json
import logging
import os.path
import random
from datetime import datetime, timezone
from functools import lru_cache

import numpy
import requests
from PIL import Image
from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

from imaginairy.utils import get_device, get_device_name

logger = logging.getLogger(__name__)


class InvalidUrlError(ValueError):
    pass


class LazyLoadingImage:
    def __init__(self, *, filepath=None, url=None):
        if not filepath and not url:
            raise ValueError("You must specify a url or filepath")
        if filepath and url:
            raise ValueError("You cannot specify a url and filepath")

        # validate file exists
        if filepath and not os.path.exists(filepath):
            raise FileNotFoundError(f"File does not exist: {filepath}")

        # validate url is valid url
        if url:
            try:
                parsed_url = parse_url(url)
            except LocationParseError:
                raise InvalidUrlError(f"Invalid url: {url}")  # noqa
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
                raise InvalidUrlError(f"Invalid url: {url}")

        self._lazy_filepath = filepath
        self._lazy_url = url
        self._img = None

    def __getattr__(self, key):
        if key == "_img":
            #  http://nedbatchelder.com/blog/201010/surprising_getattr_recursion.html
            raise AttributeError()
        if self._img:
            return getattr(self._img, key)

        if self._lazy_filepath:
            self._img = Image.open(self._lazy_filepath)
            logger.info(
                f"Loaded input 🖼  of size {self._img.size} from {self._lazy_filepath}"
            )
        elif self._lazy_url:
            self._img = Image.open(
                requests.get(self._lazy_url, stream=True, timeout=60).raw
            )
            logger.info(
                f"Loaded input 🖼  of size {self._img.size} from {self._lazy_url}"
            )

        return getattr(self._img, key)

    def __str__(self):
        return self._lazy_filepath or self._lazy_url


class WeightedPrompt:
    def __init__(self, text, weight=1):
        self.text = text
        self.weight = weight

    def __str__(self):
        return f"{self.weight}*({self.text})"


class ImaginePrompt:
    class MaskMode:
        KEEP = "keep"
        REPLACE = "replace"

    def __init__(
        self,
        prompt=None,
        prompt_strength=7.5,
        init_image=None,  # Pillow Image, LazyLoadingImage, or filepath str
        init_image_strength=0.3,
        mask_prompt=None,
        mask_image=None,
        mask_mode=MaskMode.REPLACE,
        mask_expansion=2,
        mask_separator=0.9,
        seed=None,
        steps=50,
        height=512,
        width=512,
        upscale=False,
        fix_faces=False,
        sampler_type="PLMS",
        conditioning=None,
        tile_mode=False,
    ):
        prompt = prompt if prompt is not None else "a scenic landscape"
        if isinstance(prompt, str):
            self.prompts = [WeightedPrompt(prompt, 1)]
        else:
            self.prompts = prompt
        self.prompts.sort(key=lambda p: p.weight, reverse=True)
        self.prompt_strength = prompt_strength
        if isinstance(init_image, str):
            init_image = LazyLoadingImage(filepath=init_image)
            
        if isinstance(mask_image, str):
            mask_image = LazyLoadingImage(filepath=mask_image)

        if mask_image is not None and mask_prompt is not None:
            raise ValueError("You can only set one of `mask_image` and `mask_prompt`")

        self.init_image = init_image
        self.init_image_strength = init_image_strength
        self.seed = random.randint(1, 1_000_000_000) if seed is None else seed
        self.steps = steps
        self.height = height
        self.width = width
        self.upscale = upscale
        self.fix_faces = fix_faces
        self.sampler_type = sampler_type
        self.conditioning = conditioning
        self.mask_prompt = mask_prompt
        self.mask_image = mask_image
        self.mask_mode = mask_mode
        self.mask_expansion = mask_expansion
        self.mask_separator = mask_separator
        self.tile_mode = tile_mode

    @property
    def prompt_text(self):
        if len(self.prompts) == 1:
            return self.prompts[0].text
        return "|".join(str(p) for p in self.prompts)

    def prompt_description(self):
        return (
            f'🖼  : "{self.prompt_text}" {self.width}x{self.height}px '
            f"seed:{self.seed} prompt-strength:{self.prompt_strength} steps:{self.steps} sampler-type:{self.sampler_type}"
        )

    def as_dict(self):
        prompts = [(p.weight, p.text) for p in self.prompts]
        return {
            "software": "imaginairy",
            "prompts": prompts,
            "prompt_strength": self.prompt_strength,
            "init_image": str(self.init_image),
            "init_image_strength": self.init_image_strength,
            "seed": self.seed,
            "steps": self.steps,
            "height": self.height,
            "width": self.width,
            "upscale": self.upscale,
            "fix_faces": self.fix_faces,
            "sampler_type": self.sampler_type,
        }


class ExifCodes:
    """https://www.awaresystems.be/imaging/tiff/tifftags/baseline.html"""

    ImageDescription = 0x010E
    Software = 0x0131
    DateTime = 0x0132
    HostComputer = 0x013C
    UserComment = 0x9286


class ImagineResult:
    def __init__(self, img, prompt: ImaginePrompt, is_nsfw, upscaled_img=None):
        self.img = img
        self.upscaled_img = upscaled_img
        self.prompt = prompt
        self.is_nsfw = is_nsfw
        self.created_at = datetime.utcnow().replace(tzinfo=timezone.utc)
        self.torch_backend = get_device()
        self.hardware_name = get_device_name(get_device())

    def cv2_img(self):
        open_cv_image = numpy.array(self.img)
        # Convert RGB to BGR
        open_cv_image = open_cv_image[:, :, ::-1].copy()
        return open_cv_image
        # return cv2.cvtColor(numpy.array(self.img), cv2.COLOR_RGB2BGR)

    def md5(self):
        return hashlib.md5(self.img.tobytes()).hexdigest()

    def metadata_dict(self):
        return {
            "prompt": self.prompt.as_dict(),
        }

    def _exif(self):
        exif = Image.Exif()
        exif[ExifCodes.ImageDescription] = self.prompt.prompt_description()
        exif[ExifCodes.UserComment] = json.dumps(self.metadata_dict())
        # help future web scrapes not ingest AI generated art
        exif[ExifCodes.Software] = "Imaginairy / Stable Diffusion v1.4"
        exif[ExifCodes.DateTime] = self.created_at.isoformat(sep=" ")[:19]
        exif[ExifCodes.HostComputer] = f"{self.torch_backend}:{self.hardware_name}"
        return exif

    def save(self, save_path):
        self.img.save(save_path, exif=self._exif())

    def save_upscaled(self, save_path):
        self.upscaled_img.save(save_path, exif=self._exif())


@lru_cache(maxsize=2)
def _get_briefly_cached_url(url):
    return requests.get(url, timeout=60)
