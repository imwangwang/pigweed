#!/usr/bin/env python3
# Copyright 2020 The Pigweed Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
"""Creates and manages token databases.

This module manages reading tokenized strings from ELF files and building and
maintaining token databases.
"""

import argparse
from datetime import datetime
import glob
import logging
import os
import re
import struct
import sys
from typing import Dict, Iterable

try:
    from pw_tokenizer import elf_reader, tokens
except ImportError:
    # Append this path to the module search path to allow running this module
    # without installing the pw_tokenizer package.
    sys.path.append(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from pw_tokenizer import elf_reader, tokens

_LOG = logging.getLogger('pw_tokenizer')


def _elf_reader(elf) -> elf_reader.Elf:
    return elf if isinstance(elf, elf_reader.Elf) else elf_reader.Elf(elf)


def _read_strings_from_elf(elf) -> Iterable[str]:
    """Reads the tokenized strings from an elf_reader.Elf or ELF file object."""
    sections = _elf_reader(elf).dump_sections(r'\.tokenized(\.\d+)?')
    if sections is not None:
        for string in sections.split(b'\0'):
            yield string.decode()


def read_tokenizer_metadata(elf) -> Dict[str, int]:
    """Reads the metadata entries from an ELF."""
    sections = _elf_reader(elf).dump_sections(r'\.tokenized\.meta')

    metadata: Dict[str, int] = {}
    if sections is not None:
        for key, value in struct.iter_unpack('12sI', sections):
            try:
                metadata[key.rstrip(b'\0').decode()] = value
            except UnicodeDecodeError as err:
                _LOG.error('Failed to decode metadata key %r: %s',
                           key.rstrip(b'\0'), err)

    return metadata


def _load_token_database(db) -> tokens.Database:
    """Loads a Database from a database object, ELF, CSV, or binary database."""
    if db is None:
        return tokens.Database()

    if isinstance(db, tokens.Database):
        return db

    if isinstance(db, elf_reader.Elf):
        return tokens.Database.from_strings(_read_strings_from_elf(db))

    # If it's a str, it might be a path. Check if it's an ELF or CSV.
    if isinstance(db, str):
        if not os.path.exists(db):
            raise FileNotFoundError(
                '"{}" is not a path to a token database'.format(db))

        # Read the path as an ELF file.
        with open(db, 'rb') as fd:
            if elf_reader.compatible_file(fd):
                return tokens.Database.from_strings(_read_strings_from_elf(fd))

        # Read the path as a packed binary or CSV file.
        return tokens.DatabaseFile(db)

    # Assume that it's a file object and check if it's an ELF.
    if elf_reader.compatible_file(db):
        return tokens.Database.from_strings(_read_strings_from_elf(db))

    # Read the database as CSV or packed binary from a file object's path.
    if hasattr(db, 'name') and os.path.exists(db.name):
        return tokens.DatabaseFile(db.name)

    # Read CSV directly from the file object.
    return tokens.Database(tokens.parse_csv(db))


def load_token_database(*databases) -> tokens.Database:
    """Loads a Database from database objects, ELFs, CSVs, or binary files."""
    return tokens.Database.merged(*(_load_token_database(db)
                                    for db in databases))


def generate_report(db: tokens.Database) -> Dict[str, int]:
    """Returns a simple report of properties of the database."""
    present = [entry for entry in db.entries() if not entry.date_removed]

    # Add 1 to each string's size to account for the null terminator.
    return {
        'present_entries': len(present),
        'present_size_bytes': sum(len(entry.string) + 1 for entry in present),
        'total_entries': len(db.entries()),
        'total_size_bytes':
        sum(len(entry.string) + 1 for entry in db.entries()),
        'collisions': len(db.collisions()),
    }


def _handle_create(elf_or_token_database, database, force, output_type,
                   include, exclude):
    """Creates a token database file from one or more ELF files."""

    if database == '-':
        # Must write bytes to stdout; use sys.stdout.buffer.
        fd = sys.stdout.buffer
    elif not force and os.path.exists(database):
        raise FileExistsError(
            'The file {} already exists! Use --force to overwrite.'.format(
                database))
    else:
        fd = open(database, 'wb')

    database = tokens.Database.merged(*elf_or_token_database)
    database.filter(include, exclude)

    with fd:
        if output_type == 'csv':
            tokens.write_csv(database, fd)
        elif output_type == 'binary':
            tokens.write_binary(database, fd)
        else:
            raise ValueError('Unknown database type "{}"'.format(output_type))

    _LOG.info('Wrote database with %d entries to %s as %s', len(database),
              fd.name, output_type)


def _handle_add(token_database, elf_or_token_database):
    initial = len(token_database)

    for source in elf_or_token_database:
        token_database.add((entry.string for entry in source.entries()))

    token_database.write_to_file()

    _LOG.info('Added %d entries to %s',
              len(token_database) - initial, token_database.path)


def _handle_mark_removals(token_database, elf_or_token_database, date):
    marked_removed = token_database.mark_removals(
        (entry.string
         for entry in tokens.Database.merged(*elf_or_token_database).entries()
         if not entry.date_removed), date)

    token_database.write_to_file()

    _LOG.info('Marked %d of %d entries as removed in %s', len(marked_removed),
              len(token_database), token_database.path)


def _handle_purge(token_database, before):
    purged = token_database.purge(before)
    token_database.write_to_file()

    _LOG.info('Removed %d entries from %s', len(purged), token_database.path)


def _handle_report(database, output):
    for path, db in database:
        output.write('{name}\n'
                     '        Entries present: {present_entries}\n'
                     '        Size of strings: {present_size_bytes} B\n'
                     '          Total entries: {total_entries}\n'
                     '  Total size of strings: {total_size_bytes} B\n'
                     '             Collisions: {collisions} tokens\n'.format(
                         name=path, **generate_report(db)))


def expand_paths_or_globs(paths_or_globs: Iterable[str]) -> Iterable[str]:
    """Expands any globs in a list of paths; raises FileNotFoundError."""
    for path_or_glob in paths_or_globs:
        if os.path.exists(path_or_glob):
            # This is a valid path; yield it without evaluating it as a glob.
            yield path_or_glob
        else:
            paths = glob.glob(path_or_glob)
            if not paths:
                raise FileNotFoundError(
                    '{} is not a valid path'.format(path_or_glob))

            for path in paths:
                yield path


class LoadTokenDatabase(argparse.Action):
    """Argparse action that reads tokenized logs from paths or glob patterns."""
    def __init__(self, option_strings, dest, include_paths=False, **kwargs):
        """Accepts arguments passed in add_argument.

    Args:
      option_strings: Forwarded to base argparse.Action.
      dest: The name of the argument to set; forwarded to base argparse.Action.
      include_paths: Whether to include the paths to the files for each database
        in addition to the database itself; if True, a list of (path, database)
        tuples is produced.
      **kwargs: Any other arguments to add_argument.
    """
        super(LoadTokenDatabase, self).__init__(option_strings, dest, **kwargs)

        if include_paths:  # Make a (path, tokens.Database) tuple for each path.
            self._load_db = lambda path: (path, load_token_database(path))
        else:
            self._load_db = load_token_database

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(
            namespace, self.dest,
            [self._load_db(path) for path in expand_paths_or_globs(values)])


def _parse_args():
    """Parse and return command line arguments."""
    def year_month_day(value) -> datetime:
        if value == 'today':
            return datetime.now()

        return datetime.strptime(value, tokens.DATE_FORMAT)

    year_month_day.__name__ = 'year-month-day (YYYY-MM-DD)'

    # Shared command line options.
    option_db = argparse.ArgumentParser(add_help=False)
    option_db.add_argument('-d',
                           '--database',
                           dest='token_database',
                           type=tokens.DatabaseFile,
                           required=True,
                           help='The database file to update.')

    option_tokens = argparse.ArgumentParser(add_help=False)
    option_tokens.add_argument(
        'elf_or_token_database',
        nargs='+',
        action=LoadTokenDatabase,
        help=(
            'ELF files or token database files from which to read strings and '
            'tokens.'))

    # Top-level argument parser.
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.set_defaults(handler=lambda **_: parser.print_help())

    subparsers = parser.add_subparsers(
        help='Tokenized string database management actions:')

    # The 'create' command creates a database file.
    subparser = subparsers.add_parser(
        'create',
        parents=[option_tokens],
        help=
        'Creates a database with tokenized strings from one or more sources.')
    subparser.set_defaults(handler=_handle_create)
    subparser.add_argument(
        '-d',
        '--database',
        required=True,
        help='Path to the database file to create; use - for stdout.')
    subparser.add_argument(
        '-t',
        '--type',
        dest='output_type',
        choices=('csv', 'binary'),
        default='csv',
        help='Which type of database to create. (default: csv)')
    subparser.add_argument('-f',
                           '--force',
                           action='store_true',
                           help='Overwrite the database if it exists.')
    subparser.add_argument(
        '-i',
        '--include',
        type=re.compile,
        action='append',
        help=(
            'If provided, at least one of these regular expressions must match '
            'for a string to be included in the database.'))
    subparser.add_argument(
        '-e',
        '--exclude',
        type=re.compile,
        action='append',
        help=('If provided, none of these regular expressions may match for a '
              'string to be included in the database.'))

    # The 'add' command adds strings to a database from a set of ELFs.
    subparser = subparsers.add_parser(
        'add',
        parents=[option_db, option_tokens],
        help=(
            'Adds new strings to a database with tokenized strings from a set '
            'of ELF files or other token databases. Missing entries are NOT '
            'marked as removed.'))
    subparser.set_defaults(handler=_handle_add)

    # The 'mark_removals' command marks removed entries to match a set of ELFs.
    subparser = subparsers.add_parser(
        'mark_removals',
        parents=[option_db, option_tokens],
        help=(
            'Updates a database with tokenized strings from a set of strings. '
            'Strings not present in the set remain in the database but are '
            'marked as removed. New strings are NOT added.'))
    subparser.set_defaults(handler=_handle_mark_removals)
    subparser.add_argument(
        '--date',
        type=year_month_day,
        help=('The removal date to use for all strings. '
              'May be YYYY-MM-DD or "today". (default: today)'))

    # The 'purge' command removes old entries.
    subparser = subparsers.add_parser(
        'purge',
        parents=[option_db],
        help='Purges removed strings from a database.')
    subparser.set_defaults(handler=_handle_purge)
    subparser.add_argument(
        '-b',
        '--before',
        type=year_month_day,
        help=('Delete all entries removed on or before this date. '
              'May be YYYY-MM-DD or "today".'))

    # The 'report' command prints a report about a database.
    subparser = subparsers.add_parser('report',
                                      help='Prints a report about a database.')
    subparser.set_defaults(handler=_handle_report)
    subparser.add_argument(
        'database',
        nargs='+',
        action=LoadTokenDatabase,
        include_paths=True,
        help='The ELF files or token databases about which to generate reports.'
    )
    subparser.add_argument(
        '-o',
        '--output',
        type=argparse.FileType('w'),
        default=sys.stdout,
        help='The file to which to write the output; use - for stdout.')

    return parser.parse_args()


def _init_logging(level: int) -> None:
    _LOG.setLevel(logging.DEBUG)
    log_to_stderr = logging.StreamHandler()
    log_to_stderr.setLevel(level)
    log_to_stderr.setFormatter(
        logging.Formatter(
            fmt='%(asctime)s.%(msecs)03d-%(levelname)s: %(message)s',
            datefmt='%H:%M:%S'))

    _LOG.addHandler(log_to_stderr)


def _main(args: argparse.Namespace) -> int:
    _init_logging(logging.INFO)

    handler = args.handler
    del args.handler

    handler(**vars(args))
    return 0


if __name__ == '__main__':
    sys.exit(_main(_parse_args()))
