import matplotlib
matplotlib.use('Agg')
from chimp import constants
from chimp.grid import GridImages
from chimp import plotting
from collections import Counter, defaultdict
import fastqimagealigner
import functools
import h5py
import itertools
import logging
import multiprocessing
from multiprocessing import Manager
import os
import sys
import re

log = logging.getLogger(__name__)
stats_regex = re.compile(r'''^(\w+)_(?P<row>\d+)_(?P<column>\d+)_stats\.txt$''')


def run_second_channel(h5_filenames, alignment_parameters, all_tile_data,
                       experiment, um_per_pixel, channel, alignment_channel, make_pdfs):
    num_processes = multiprocessing.cpu_count()
    log.debug("Doing second channel alignment of all images with %d cores" % num_processes)
    for h5_filename, base_name, stats_filepath in load_aligned_stats_files(h5_filenames, alignment_channel, experiment):
        try:
            row, column = extract_rc_info(stats_filepath)
        except ValueError:
            log.warn("Invalid stats file: %s" % str(stats_filepath))
            continue
        with h5py.File(h5_filename) as h5:
            grid = GridImages(h5, channel)
            image = grid.get(row, column)
            process_data_image(alignment_parameters, all_tile_data, um_per_pixel, experiment,
                               make_pdfs, base_name, image, stats_filepath)
    log.debug("Done aligning!")


def extract_rc_info(stats_file):
    match = stats_regex.match(stats_file)
    if match:
        return int(match.group('row')), int(match.group('column'))
    raise ValueError("Invalid stats file: %s" % str(stats_file))


def load_aligned_stats_files(h5_filenames, channel, experiment):
    for h5_filename in h5_filenames:
        base_name = os.path.splitext(h5_filename)[0]
        for f in os.listdir(os.path.join(experiment.results_directory, base_name)):
            if f.endswith('_stats.txt') and channel in f:
                yield h5_filename, base_name, f


def process_data_image(alignment_parameters, tile_data, um_per_pixel, experiment, make_pdfs, base_name, image, stats_filepath):
    sexcat_filepath = os.path.join(base_name, '%s.cat' % image.index)
    stats_filepath = os.path.join(experiment.results_directory, base_name, stats_filepath)
    fastq_image_aligner = fastqimagealigner.FastqImageAligner(experiment)
    fastq_image_aligner.load_reads(tile_data)
    fastq_image_aligner.set_image_data(image, um_per_pixel)
    fastq_image_aligner.set_sexcat_from_file(sexcat_filepath)
    fastq_image_aligner.alignment_from_alignment_file(stats_filepath)
    fastq_image_aligner.precision_align_only(min_hits=alignment_parameters.min_hits)
    log.debug("Processed 2nd channel for %s" % image.index)
    write_output(image.index, base_name, fastq_image_aligner, experiment, tile_data, make_pdfs)


def run(h5_filenames, alignment_parameters, alignment_tile_data, all_tile_data, experiment,
        um_per_pixel, channel):
    assert len(h5_filenames) > 0
    right_side_tiles = [format_tile_number(2100 + num) for num in range(1, 11)]
    left_side_tiles = [format_tile_number(2100 + num) for num in reversed(range(11, 20))]

    # We use one process per concentration. We could theoretically speed this up since our machine
    # has significantly more cores than the typical number of concentration points, but since it
    # usually finds a result in the first image or two, it's not going to deliver any practical benefits
    num_processes = len(h5_filenames)
    pool = multiprocessing.Pool(num_processes)
    fia = fastqimagealigner.FastqImageAligner(experiment)
    fia.load_reads(alignment_tile_data)

    with h5py.File(h5_filenames[0]) as first_file:
        grid = GridImages(first_file, channel)
        # find columns/tiles on the left side

        base_column_checker = functools.partial(check_column_for_alignment, channel, alignment_parameters,
                                                alignment_tile_data, um_per_pixel, experiment, fia)

        left_end_tiles = dict(get_bounds(pool, h5_filenames, base_column_checker, grid.columns, left_side_tiles))
        right_end_tiles = dict(get_bounds(pool, h5_filenames, base_column_checker, reversed(grid.columns), right_side_tiles))

    default_left_tile, default_left_column = decide_default_tiles_and_columns(left_end_tiles)
    default_right_tile, default_right_column = decide_default_tiles_and_columns(right_end_tiles)

    end_tiles = {}
    # Now build up the end tile data structure
    for filename in h5_filenames:
        try:
            left_tiles, left_column = left_end_tiles[filename]
        except KeyError:
            left_tiles, left_column = [default_left_tile], default_left_column
        try:
            right_tiles, right_column = right_end_tiles[filename]
        except KeyError:
            right_tiles, right_column = [default_right_tile], default_right_column
        min_column, max_column = min(left_column, right_column), max(left_column, right_column)
        tile_map = get_expected_tile_map(left_tiles, right_tiles, min_column, max_column)
        end_tiles[filename] = min_column, max_column, tile_map

    # Iterate over images that are probably inside an Illumina tile, attempt to align them, and if they
    # align, do a precision alignment and write the mapped FastQ reads to disk
    num_processes = multiprocessing.cpu_count()
    log.debug("Aligning all images with %d cores" % num_processes)
    alignment_func = functools.partial(perform_alignment, alignment_parameters, um_per_pixel,
                                       experiment, alignment_tile_data, all_tile_data)
    pool = multiprocessing.Pool(num_processes)
    pool.map_async(alignment_func,
                   iterate_all_images(h5_filenames, end_tiles, channel)).get(timeout=sys.maxint)
    log.debug("Done aligning!")


def decide_default_tiles_and_columns(end_tiles):
    all_tiles = []
    columns = []
    for filename, (tiles, column) in end_tiles.items():
        for tile in tiles:
            all_tiles.append(tile)
        columns.append(column)
    a, b = Counter(all_tiles).most_common(1), Counter(columns).most_common(1)
    best_tile, best_column = a[0][0], b[0][0]
    return best_tile, best_column


def get_bounds(pool, h5_filenames, base_column_checker, columns, possible_tile_keys):
    end_tiles = Manager().dict()
    for column in columns:
        column_checker = functools.partial(base_column_checker, end_tiles, column, possible_tile_keys)
        pool.map_async(column_checker, h5_filenames).get(sys.maxint)
        if end_tiles:
            return end_tiles
    return False


def check_column_for_alignment(channel, alignment_parameters, alignment_tile_data, um_per_pixel,
                               experiment, fia, end_tiles, column, possible_tile_keys, h5_filename):
    base_name = os.path.splitext(h5_filename)[0]
    with h5py.File(h5_filename) as h5:
        grid = GridImages(h5, channel)
        image = grid.get(3, column)
        fia = process_alignment_image(alignment_parameters, base_name, alignment_tile_data,
                                      um_per_pixel, experiment, image, possible_tile_keys,
                                      preloaded_fia=fia)
        if fia.hitting_tiles:
            log.debug("%s aligned to at least one tile!" % image.index)
            # because of the way we iterate through the images, if we find one that aligns,
            # we can just stop because that gives us the outermost column of images and the
            # outermost FastQ tile
            end_tiles[h5_filename] = [tile.key for tile in fia.hitting_tiles], image.column


def perform_alignment(alignment_parameters, um_per_pixel, experiment, alignment_tile_data,
                      all_tile_data, image_data, make_pdfs):
    # Does a rough alignment, and if that works, does a precision alignment and writes the corrected
    # FastQ reads to disk
    row, column, channel, h5_filename, possible_tile_keys, base_name = image_data
    # image, possible_tile_keys, base_name = image_data
    with h5py.File(h5_filename) as h5:
        grid = GridImages(h5, channel)
        image = grid.get(row, column)
    log.debug("Aligning image from %s. Row: %d, Column: %d " % (base_name, image.row, image.column))
    # first get the correlation to random tiles, so we can distinguish signal from noise
    fia = process_alignment_image(alignment_parameters, base_name, alignment_tile_data,  um_per_pixel,
                                  experiment, image, possible_tile_keys)
    if fia.hitting_tiles:
        # The image data aligned with FastQ reads!
        try:
            fia.precision_align_only(hit_type=('exclusive', 'good_mutual'),
                                     min_hits=alignment_parameters.min_hits)
        except AssertionError:
            log.debug("Too few hits to perform precision alignment. Image: %s Row: %d Column: %d " % (base_name, image.row, image.column))
        else:
            write_output(image.index, base_name, fia, experiment, all_tile_data, make_pdfs)
    # The garbage collector takes its sweet time for some reason, so we have to manually delete
    # these objects or memory usage blows up.
    del fia
    del image


def iterate_all_images(h5_filenames, end_tiles, channel):
    # We need an iterator over all images to feed the parallel processes. Since each image is
    # processed independently and in no particular order, we need to return information in addition
    # to the image itself that allow files to be written in the correct place and such
    for h5_filename in h5_filenames:
        base_name = os.path.splitext(h5_filename)[0]
        with h5py.File(h5_filename) as h5:
            grid = GridImages(h5, channel)
            min_column, max_column, tile_map = end_tiles[h5_filename]
            for column in range(min_column, max_column):
                for row in range(grid._height):
                    image = grid.get(row, column)
                    if image is not None:
                        yield row, column, channel, h5_filename, tile_map[image.column], base_name

def load_read_names(file_path):
    # reads a FastQ file with Illumina read names
    with open(file_path) as f:
        tiles = defaultdict(set)
        for line in f:
            lane, tile = line.strip().rsplit(':', 4)[1:3]
            key = 'lane{0}tile{1}'.format(lane, tile)
            tiles[key].add(line.strip())
    del f
    return {key: list(values) for key, values in tiles.items()}


def get_expected_tile_map(left_tiles, right_tiles, min_column, max_column):
    # Creates a dictionary that relates each column of microscope images to its expected tile, +/- 1.
    # Works regardless of whether everything is flipped upsidedown (i.e. the lower tile is on the
    # right side)
    tile_map = defaultdict(list)

    # We gets lists of tiles, so we have to work out the minimum and maximum number in a slightly
    # complicated way
    left_tiles = [int(tile[-4:]) for tile in left_tiles]
    right_tiles = [int(tile[-4:]) for tile in right_tiles]
    min_tile = min(itertools.chain(left_tiles, right_tiles))
    max_tile = max(itertools.chain(left_tiles, right_tiles))

    # Keep track of whether we'll have to invert all the associations of tiles and columns
    invert_map = True if min_tile not in left_tiles else False

    # Find the "tiles per column" factor so we can map a column to a tile
    normalization_factor = float(abs(max_tile - min_tile) + 1) / float(max_column - min_column)

    # Build up the map
    for column in range(min_column, max_column + 1):
        expected = int(round(normalization_factor * column)) - 1
        expected = min(constants.MISEQ_TILE_COUNT, max(0, expected)) + min_tile
        tile_map_column = column if not invert_map else max_column - column
        # We definitely need to check the predicted tile
        tile_map[tile_map_column].append(format_tile_number(expected))
        # If we're at a boundary, we just want to check the adjacent tile towards the middle
        # If we're in the middle, we want to check the tiles on either side
        if expected < max_tile:
            tile_map[tile_map_column].append(format_tile_number(expected + 1))
        if expected > min_tile:
            tile_map[tile_map_column].append(format_tile_number(expected - 1))
    return tile_map


def format_tile_number(number):
    # this definitely looks like a temporary hack that will end up becoming the most enduring
    # part of this codebase
    return 'lane1tile{0}'.format(number)


def process_alignment_image(alignment_parameters, base_name, tile_data,
                um_per_pixel, experiment, image, possible_tile_keys, preloaded_fia=None):
    for directory in (experiment.figure_directory, experiment.results_directory):
        full_directory = os.path.join(directory, base_name)
        if not os.path.exists(full_directory):
            os.makedirs(full_directory)
    sexcat_fpath = os.path.join(base_name, '%s.cat' % image.index)
    if preloaded_fia is None:
        fia = fastqimagealigner.FastqImageAligner(experiment)
        fia.load_reads(tile_data)
    else:
        fia = preloaded_fia
    fia.set_image_data(image, um_per_pixel)
    fia.set_sexcat_from_file(sexcat_fpath)
    fia.rough_align(possible_tile_keys,
                    alignment_parameters.rotation_estimate,
                    alignment_parameters.fastq_tile_width_estimate,
                    snr_thresh=alignment_parameters.snr_threshold)
    return fia




def write_output(image_index, base_name, fastq_image_aligner, experiment, tile_data, make_pdfs):
    intensity_filepath = os.path.join(experiment.results_directory,
                                      base_name, '{}_intensities.txt'.format(image_index))
    stats_filepath = os.path.join(experiment.results_directory,
                                  base_name, '{}_stats.txt'.format(image_index))
    all_read_rcs_filepath = os.path.join(experiment.results_directory,
                                         base_name, '{}_all_read_rcs.txt'.format(image_index))

    if make_pdfs:
        ax = plotting.plot_all_hits(fastq_image_aligner)
        ax.figure.savefig(os.path.join(experiment.figure_directory, '{}_all_hits.pdf'.format(image_index)))
        ax = plotting.plot_hit_hists(fastq_image_aligner)
        ax.figure.savefig(os.path.join(experiment.figure_directory, '{}_hit_hists.pdf'.format(image_index)))

    fastq_image_aligner.output_intensity_results(intensity_filepath)
    fastq_image_aligner.write_alignment_stats(stats_filepath)
    all_fastq_image_aligner = fastqimagealigner.FastqImageAligner(experiment)
    all_fastq_image_aligner.all_reads_fic_from_aligned_fic(fastq_image_aligner, tile_data)
    all_fastq_image_aligner.write_read_names_rcs(all_read_rcs_filepath)
