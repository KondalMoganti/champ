from abc import abstractproperty, abstractmethod
import os
import re
import tifffile
from collections import defaultdict
import numpy as np
import logging

log = logging.getLogger(__name__)

whitespace_regex = re.compile('[\s]+')
special_chars_regex = re.compile('[\W]+')
name_regex = re.compile(r"""[\w-]+Pos_(\d+)_(\d+)""")


def sanitize_name(name):
    return special_chars_regex.sub('', whitespace_regex.sub('_', name))


class BaseTifStack(object):
    def __init__(self, filenames, adjustments):
        self._filenames = filenames
        self._adjustments = adjustments
        self._axes = {}

    @abstractproperty
    def axes(self):
        raise NotImplementedError

    @abstractmethod
    def __iter__(self):
        raise NotImplementedError


class TifsPerFieldOfView(BaseTifStack):
    """
    This class handles the scenario where MicroManager creates a separate TIF for each image taken during a 
    multidimensional acquisition.
    
    """
    @property
    def axes(self):
        if not self._axes:
            tif_axes = {}
            best_first = 0
            best_second = 0
            for file_path in self._filenames:
                filename = os.path.split(file_path)[1]
                axis_positions = name_regex.search(filename)
                first = int(axis_positions.group(1))
                second = int(axis_positions.group(2))
                best_first = max(first, best_first)
                best_second = max(second, best_second)
                tif_axes[file_path] = (first, second)
            if best_second > best_first:
                # the second thing is the major axis, so we need to invert them
                self._axes = {file_path: (second, first) for file_path, (first, second) in tif_axes.items()}
            # no need to invert, just return the values we already have
            else:
                self._axes = tif_axes
        return self._axes

    def __iter__(self):
        first_filename = self._filenames[0]
        with tifffile.TiffFile(first_filename) as tif:
            summary = tif.micromanager_metadata['summary']
            height, width = summary['Height'], summary['Width']
            if height % 512 != 0 or width % 512 != 0:
                raise ValueError("CHAMP currently only supports images with sides that are multiples of 512 pixels.")
            # if the images are larger than 512x512, we need to subdivide them
            subrows, subcolumns = range(height / 512), range(width / 512)

            for file_path in self._filenames:
                major_axis_position, minor_axis_position = self.axes[file_path]
                for subrow in subrows:
                    # TODO: THESE NUMBERS ARE WRONG, THEY'RE BASED ON RANDO NUMBERING IN THE ONE EXAMPLE WE HAVE
                    # TODO: WE ASSUME BOTH INDEXES START AT ZERO - THAT'S NOT ALWAYS TRUE
                    minor_axis_label = (minor_axis_position * len(subrows)) - len(subrows) + subrow
                    for subcolumn in subcolumns:
                        major_axis_label = (major_axis_position * len(subcolumns)) - len(subcolumns) + subcolumn + 2
                        dataset_name = '(Major, minor) = ({}, {})'.format(major_axis_label, minor_axis_label)

                        with tifffile.TiffFile(file_path) as tif:
                            summary = tif.micromanager_metadata['summary']

                            # Find channel names and assert unique
                            channel_names = [sanitize_name(name) for name in summary['ChNames']]
                            assert summary['Channels'] == len(channel_names) == len(set(channel_names)), channel_names

                            # channel_idxs map tif pages to channels
                            channels = [channel_names[i] for i in tif.micromanager_metadata['index_map']['channel']]

                            # Setup defaultdict
                            height, width = summary['Height'], summary['Width']
                            summed_images = defaultdict(lambda *x: np.zeros((512, 512), dtype=np.int))

                            # Add images
                            for channel, page in zip(channels, tif.pages):
                                image = page.asarray()
                                # this subdivision might be incorrect formally, it might be putting them in the wrong part of the larger "box"
                                image = image[subrow * 512: (subrow * 512) + 512, subcolumn * 512: (subcolumn * 512) + 512]
                                for adjustment in self._adjustments:
                                    image = adjustment(image)
                                summed_images[channel] += image
                            yield TIFSingleFieldOfView(summed_images, dataset_name)


class TifsPerConcentration(BaseTifStack):
    """
    This class handles the scenario where MicroManager can put all images taken during a multidimensional acquisition 
    into a single file. 
     
    """
    @property
    def axes(self):
        if not self._axes:
            best_first = 0
            best_second = 0
            for file_path in self._filenames:
                tif_axes = {}
                log.debug("Loading position list from %s" % file_path)
                with tifffile.TiffFile(file_path) as tif_stack:
                    for tif in tif_stack:
                        position_text = tif.micromanager_metadata['PositionName']
                        axis_positions = name_regex.search(position_text)
                        if not axis_positions:
                            print("FAIL: %s" % position_text)
                        else:
                            first = int(axis_positions.group(1))
                            second = int(axis_positions.group(2))
                            best_first = max(first, best_first)
                            best_second = max(second, best_second)
                            tif_axes[position_text] = (first, second)
                if best_second > best_first:
                    # the second thing is the major axis, so we need to invert them
                    tif_axes = {position_text: (second, first) for position_text, (first, second) in tif_axes.items()}
                # no need to invert, just return the values we already have
                self._axes[file_path] = tif_axes
        return self._axes

    def __iter__(self):
        for file_path in self._filenames:
            all_pages = defaultdict(list)
            with tifffile.TiffFile(file_path) as tif:
                summary = tif.micromanager_metadata['summary']

                # if the images are large, we need to break them up
                height, width = summary['Height'], summary['Width']
                if height % 512 != 0 or width % 512 != 0:
                    raise ValueError("CHAMP currently only supports images with sides that are multiples of 512 pixels.")

                # if the images are larger than 512x512, we need to subdivide them
                subrows, subcolumns = range(height / 512), range(width / 512)
                # Find channel names and assert unique
                channel_names = [sanitize_name(name) for name in summary['ChNames']]
                assert summary['Channels'] == len(channel_names) == len(set(channel_names)), channel_names
                # channel_idxs map tif pages to channels
                channels = [channel_names[i] for i in tif.micromanager_metadata['index_map']['channel']]
                # Setup defaultdict
                height, width = summary['Height'], summary['Width']

                for channel, page in zip(channels, tif):
                    all_pages[page.micromanager_metadata['PositionName']].append((channel, page))

                for position_text, channel_pages in all_pages.items():
                    major_axis_position, minor_axis_position = self.axes[file_path][position_text]
                    for subrow in subrows:
                        minor_axis_label = (minor_axis_position * len(subrows)) - len(subrows) + subrow + 1
                        for subcolumn in subcolumns:
                            major_axis_label = (major_axis_position * len(subcolumns)) - len(subcolumns) + subcolumn + 1
                            dataset_name = '(Major, minor) = ({}, {})'.format(major_axis_label, minor_axis_label)
                            summed_images = defaultdict(lambda *x: np.zeros((512, 512), dtype=np.int))
                            # Add images
                            for channel, page in channel_pages:
                                image = page.asarray()
                                # this subdivision might be incorrect formally, it might be putting them in the wrong part of the larger "box"
                                image = image[subrow * 512: (subrow * 512) + 512, subcolumn * 512: (subcolumn * 512) + 512]
                                for adjustment in self._adjustments:
                                    image = adjustment(image)
                                summed_images[channel] += image
                            yield TIFSingleFieldOfView(summed_images, dataset_name)


class TIFSingleFieldOfView(object):
    """
    Contains images and metadata for a single field of view at a single concentration.

    """
    def __init__(self, summed_images, dataset_name):
        self._summed_images = summed_images
        self._dataset_name = dataset_name

    @property
    def dataset_name(self):
        return self._dataset_name

    def __repr__(self):
        return "<TIFSingleFieldOfView %s>" % self._dataset_name

    def __iter__(self):
        for channel, image in self._summed_images.items():
            yield channel, image

    @property
    def channels(self):
        return self._summed_images.keys()