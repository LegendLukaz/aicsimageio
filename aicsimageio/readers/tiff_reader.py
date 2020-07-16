#!/usr/bin/env python
# -*- coding: utf-8 -*-

import io
import logging
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

import dask.array as da
import numpy as np
from dask import delayed
from tifffile import TiffFile

from .. import types
from ..buffer_reader import BufferReader
from ..constants import Dimensions
from .reader import Reader

###############################################################################

log = logging.getLogger(__name__)

###############################################################################

class TiffProperties(NamedTuple):
    dims: str
    shape: Tuple[int]
    dtype: np.dtype


class TiffReader(Reader):
    """
    TiffReader wraps tifffile to provide the same reading capabilities but abstracts
    the specifics of using the backend library to create a unified interface. This
    enables higher level functions to duck type the File Readers.

    Parameters
    ----------
    data: types.FileLike
        A string or path to the TIFF file to be read.
    S: int
        If the image has different dimensions on any scene from another, the dask array
        construction will fail.
        In that case, use this parameter to specify a specific scene to construct a
        dask array for.
        Default: 0 (select the first scene)
    """

    def __init__(self, data: types.FileLike, S: int = 0, **kwargs):
        # Run super init to check filepath provided
        super().__init__(data, **kwargs)

        # Store parameters needed for dask read
        self.specific_s_index = S

        # Lazy load and hold on to dtype and shape
        self._dtype = None
        self._shape = None

    @staticmethod
    def _is_this_type(buffer: io.BufferedIOBase) -> bool:
        with BufferReader(buffer) as buffer_reader:
            # Per the TIFF-6 spec
            # (https://www.itu.int/itudoc/itu-t/com16/tiff-fx/docs/tiff6.pdf),
            # 'II' is little-endian (Intel format)
            # 'MM' is big-endian (Motorola format)
            if buffer_reader.endianness not in [
                buffer_reader.INTEL_ENDIAN,
                buffer_reader.MOTOROLA_ENDIAN,
            ]:
                return False
            magic = buffer_reader.read_uint16()

            # Per TIFF-6, magic is 42.
            if magic == 42:
                ifd_offset = buffer_reader.read_uint32()
                if ifd_offset == 0:
                    return False

            # Per BigTIFF
            # (https://www.awaresystems.be/imaging/tiff/bigtiff.html), magic is 43.
            if magic == 43:
                # Alex magic here...
                if buffer_reader.read_uint16() != 8:
                    return False
                if buffer_reader.read_uint16() != 0:
                    return False
                ifd_offset = buffer_reader.read_uint64()
                if ifd_offset == 0:
                    return False
            return True

    @staticmethod
    def _imread(img: Path, scene: int, page: int) -> np.ndarray:
        # Load Tiff
        with TiffFile(img) as tiff:
            # Get proper scene
            scene = tiff.series[scene]

            # Get proper page
            page = scene.pages[page]

            # Return numpy
            return page.asarray()

    @staticmethod
    def _scene_shape_is_consistent(tiff: TiffFile, S: int) -> bool:
        scenes = tiff.series
        operating_shape = scenes[0].shape
        for scene in scenes:
            if scene.shape != operating_shape:
                log.info(
                    f"File contains variable dimensions per scene, "
                    f"selected scene: {S} for data "
                    f"retrieval."
                )
                return False

        return True

    def _read_delayed(self) -> da.core.Array:
        # Load Tiff
        with TiffFile(self._file) as tiff:
            # Check each scene has the same shape
            # If scene shape checking fails, use the specified scene and update
            # operating shape
            scenes = tiff.series
            operating_shape = scenes[0].shape
            if not self._scene_shape_is_consistent(tiff, S=self.specific_s_index):
                operating_shape = scenes[self.specific_s_index].shape
                scenes = [scenes[self.specific_s_index]]

            # Get sample yx plane
            sample = scenes[0].pages[0].asarray()

            # Combine length of scenes and operating shape
            # Replace YX dims with empty dimensions
            operating_shape = (len(scenes), *operating_shape)
            operating_shape = operating_shape[:-2] + (1, 1)

            # Make ndarray for lazy arrays to fill
            lazy_arrays = np.ndarray(operating_shape, dtype=object)
            for all_page_index, (np_index, _) in enumerate(np.ndenumerate(lazy_arrays)):
                # Scene index is the first index in np_index
                scene_index = np_index[0]

                # This page index is current enumeration divided by scene index + 1
                # For example if the image has 10 Z slices and 5 scenes, there
                # would be 50 total pages
                this_page_index = all_page_index // (scene_index + 1)

                # Fill the numpy array with the delayed arrays
                lazy_arrays[np_index] = da.from_delayed(
                    delayed(TiffReader._imread)(
                        self._file, scene_index, this_page_index
                    ),
                    shape=sample.shape,
                    dtype=sample.dtype,
                )

            # Convert the numpy array of lazy readers into a dask array
            data = da.block(lazy_arrays.tolist())

            # Only return the scene dimension if multiple scenes are present
            if len(scenes) == 1:
                data = data[0, :]

            return data

    def _read_immediate(self) -> np.ndarray:
        # Load Tiff
        with TiffFile(self._file) as tiff:
            # Check each scene has the same shape
            # If scene shape checking fails, use the specified scene and update
            # operating shape
            scenes = tiff.series
            if not self._scene_shape_is_consistent(tiff, S=self.specific_s_index):
                return scenes[self.specific_s_index].asarray()

            # Read each scene and stack if single scene
            if len(scenes) > 1:
                return np.stack([s.asarray() for s in scenes])

            # Else, return single scene
            return tiff.asarray()

    @staticmethod
    def _guess_tiff_dims(tiff: TiffFile, S: int) -> str:
        guess = self.guess_dim_order(tiff.series[S].pages.shape)
        best_guess = []
        for dim_from_meta in single_scene_dims:
            if dim_from_meta in Dimensions.DefaultOrder:
                best_guess.append(dim_from_meta)
            else:
                appended_dim = False
                for guessed_dim in guess:
                    if guessed_dim not in best_guess:
                        best_guess.append(guessed_dim)
                        appended_dim = True
                        log.info(
                            f"Unsure how to handle dimension: "
                            f"{dim_from_meta}. "
                            f"Replaced with guess: {guessed_dim}"
                        )
                        break

                # All of our guess dims were already in the dim list,
                # append the dim read from meta
                if not appended_dim:
                    best_guess.append(dim_from_meta)

        return "".join(best_guess)

    @staticmethod
    def _get_tiff_properties(f: types.FileLike, S: int) -> TiffProperties:
        # Get a single scenes dimensions in order
        with TiffFile(f) as tiff:
            single_scene_dims = tiff.series[S].pages.axes
            single_scene_shape = tiff.series[S].pages.shape
            dtype = tiff.pages[0].dtype

            # We can sometimes trust the dimension info in the image
            if all([d in Dimensions.DefaultOrder for d in single_scene_dims]):
                # Add scene dimension only if there are multiple scenes
                if (
                    len(tiff.series) > 1
                    and TiffReader._scene_shape_is_consistent(tiff, S)
                ):
                    dims = f"{Dimensions.Scene}{single_scene_dims}"
                    shape = (len(tiff.series), *single_scene_shape)
                else:
                    dims = single_scene_dims
                    shape = single_scene_shape

            # Sometimes the dimension info is wrong in certain dimensions, so guess
            # that dimension
            else:
                dims_best_guess = TiffReader._guess_tiff_dims(tiff, S)

                # Add scene dimension only if there are multiple scenes
                # and only when scene dimensions are consistent shape
                if (
                    len(tiff.series) > 1
                    and TiffReader._scene_shape_is_consistent(tiff, S)
                ):
                    dims = f"{Dimensions.Scene}{dims_best_guess}"
                    shape = (len(tiff.series), *single_scene_shape)
                else:
                    dims = dims_best_guess
                    shape = single_scene_shape

        return TiffProperties(dims, shape, dtype)

    @Reader.dims.getter
    def dims(self) -> str:
        if self._dims is None:
            props = self._get_tiff_properties(self._file, self.specific_s_index)
            self._dims = props.dims
            self._shape = props.shape
            self._dtype = props.dtype

        return self._dims

    @property
    def shape(self) -> Tuple[int]:
        if self._shape is None:
            props = self._get_tiff_properties(self._file, self.specific_s_index)
            self._dims = props.dims
            self._shape = props.shape
            self._dtype = props.dtype

        return self._shape

    @property
    def dtype(self):
        if self._dtype is None:
            props = self._get_tiff_properties(self._file, self.specific_s_index)
            self._dims = props.dims
            self._shape = props.shape
            self._dtype = props.dtype

        return self._dtype

    @staticmethod
    def get_image_description(buffer: io.BufferedIOBase) -> Optional[bytearray]:
        """Retrieve the image description as one large string."""
        description_length = 0
        description_offset = 0

        with BufferReader(buffer) as buffer_reader:
            # Per the TIFF-6 spec
            # (https://www.itu.int/itudoc/itu-t/com16/tiff-fx/docs/tiff6.pdf),
            # 'II' is little-endian (Intel format)
            # 'MM' is big-endian (Motorola format)
            if buffer_reader.endianness not in [
                buffer_reader.INTEL_ENDIAN,
                buffer_reader.MOTOROLA_ENDIAN,
            ]:
                return None
            magic = buffer_reader.read_uint16()

            # Per TIFF-6, magic is 42.
            if magic == 42:
                found = False
                while not found:
                    ifd_offset = buffer_reader.read_uint32()
                    if ifd_offset == 0:
                        return None
                    buffer_reader.buffer.seek(ifd_offset, 0)
                    entries = buffer_reader.read_uint16()
                    for n in range(0, entries):
                        tag = buffer_reader.read_uint16()
                        type = buffer_reader.read_uint16()
                        count = buffer_reader.read_uint32()
                        offset = buffer_reader.read_uint32()
                        if tag == 270:
                            description_length = count - 1  # drop the NUL from the end
                            description_offset = offset
                            found = True
                            break

            # Per BigTIFF
            # (https://www.awaresystems.be/imaging/tiff/bigtiff.html), magic is 43.
            if magic == 43:
                # Alex magic here...
                if buffer_reader.read_uint16() != 8:
                    return None
                if buffer_reader.read_uint16() != 0:
                    return None
                found = False
                while not found:
                    ifd_offset = buffer_reader.read_uint64()
                    if ifd_offset == 0:
                        return None
                    buffer_reader.buffer.seek(ifd_offset, 0)
                    entries = buffer_reader.read_uint64()
                    for n in range(0, entries):
                        tag = buffer_reader.read_uint16()
                        type = buffer_reader.read_uint16()  # noqa: F841
                        count = buffer_reader.read_uint64()
                        offset = buffer_reader.read_uint64()
                        if tag == 270:
                            description_length = count - 1  # drop the NUL from the end
                            description_offset = offset
                            found = True
                            break

            if description_offset == 0:
                # Nothing was found
                return bytearray("")
            else:
                buffer_reader.buffer.seek(description_offset, 0)
                return bytearray(buffer_reader.buffer.read(description_length))

    @property
    def metadata(self) -> str:
        if self._metadata is None:
            with open(self._file, "rb") as rb:
                description = self.get_image_description(rb)
            if description is None:
                self._metadata = ""
            else:
                self._metadata = description.decode()
        return self._metadata
