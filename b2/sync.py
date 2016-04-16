######################################################################
#
# File: b2/sync.py
#
# Copyright 2016 Backblaze Inc. All Rights Reserved.
#
# License https://www.backblaze.com/using_b2_code.html
#
######################################################################

import os
import sys
import threading
import time
from abc import (ABCMeta, abstractmethod)

import six

from .exception import DestFileNewer

try:
    import concurrent.futures as futures
except:
    import futures

ONE_DAY_IN_MS = 24 * 60 * 60 * 1000


class SyncReport(object):
    """
    Handles reporting progress for syncing.

    Prints out each file as it is processed, and puts up a sequence
    of progress bars.

    The progress bars are:
       - Step 1/1: count local files
       - Step 2/2: compare file lists
       - Step 3/3: transfer files

    This class is THREAD SAFE so that it can be used from parallel sync threads.
    """

    STATE_LOCAL = 'local'
    STATE_COMPARE = 'compare'
    STATE_TRANSFER = 'transfer'
    STATE_DONE = 'done'

    def __init__(self):
        self.start_time = time.time()
        self.state = 'local'
        self.local_file_count = 0
        self.compare_count = 0
        self.total_transfer_files = 0  # set in end_compare()
        self.total_transfer_bytes = 0  # set in end_compare()
        self.transfer_files = 0
        self.transfer_bytes = 0
        self.current_line = ''
        self.lock = threading.Lock()
        self._update_progress()

    def close(self):
        with self.lock:
            if self.state != self.STATE_DONE:
                self._print_line('', False)

    def print_completion(self, message):
        """
        Removes the progress bar, prints a message, and puts the progress
        bar back.
        """
        with self.lock:
            self._print_line(message, True)
            self._update_progress()

    def _update_progress(self):
        rate = int(self.transfer_bytes / (time.time() - self.start_time))
        if self.state == self.STATE_LOCAL:
            message = ' count: %d files   compare: %d files   transferred: %d files   %d bytes   %d B/s' % (
                self.local_file_count, self.compare_count, self.transfer_files, self.transfer_bytes,
                rate
            )
        elif self.state == self.STATE_COMPARE:
            message = ' compare: %d/%d files   transferred: %d files   %d bytes   %d B/s' % (
                self.compare_count, self.local_file_count, self.transfer_files, self.transfer_bytes,
                rate
            )
        elif self.state == self.STATE_TRANSFER:
            message = ' compare: %d/%d files   transferred: %d/%d files   %d/%d bytes   %d B/s' % (
                self.compare_count, self.local_file_count, self.transfer_files,
                self.total_transfer_files, self.transfer_bytes, self.total_transfer_bytes, rate
            )
        else:
            message = ''
        self._print_line(message, False)

    def _print_line(self, line, newline):
        """
        Prints a line to stdout.

        :param line: A string without a \r or \n in it.
        :param newline: True if the output should move to a new line after this one.
        """
        if len(line) < len(self.current_line):
            line += ' ' * (len(self.current_line) - len(line))
        sys.stdout.write(line)
        if newline:
            sys.stdout.write('\n')
            self.current_line = ''
        else:
            sys.stdout.write('\r')
            self.current_line = line
        sys.stdout.flush()

    def update_local(self, delta):
        """
        Reports that more local files have been found.
        """
        with self.lock:
            assert self.state == self.STATE_LOCAL
            self.local_file_count += delta
            self._update_progress()

    def end_local(self):
        """
        Local file count is done.  Can proceed to step 2.
        """
        with self.lock:
            assert self.state == self.STATE_LOCAL
            self.state = self.STATE_COMPARE
            self._update_progress()

    def update_compare(self, delta):
        """
        Reports that more files have been compared.
        """
        with self.lock:
            self.compare_count += delta
            self._update_progress()

    def end_compare(self, total_transfer_files, total_transfer_bytes):
        with self.lock:
            assert self.state == self.STATE_COMPARE
            self.state = self.STATE_TRANSFER
            self.total_transfer_files = total_transfer_files
            self.total_transfer_bytes = total_transfer_bytes
            self._update_progress()

    def update_transfer(self, file_delta, byte_delta):
        with self.lock:
            self.transfer_files += file_delta
            self.transfer_bytes += byte_delta
            self._update_progress()


def sample_sync_report_run():
    sync_report = SyncReport()

    for i in six.moves.range(20):
        sync_report.update_local(1)
        time.sleep(0.2)
        if i == 10:
            sync_report.print_completion('transferred: a.txt')
        if i % 2 == 0:
            sync_report.update_compare(1)
    sync_report.end_local()

    for i in six.moves.range(10):
        sync_report.update_compare(1)
        time.sleep(0.2)
        if i == 3:
            sync_report.print_completion('transferred: b.txt')
        if i == 4:
            sync_report.update_transfer(25, 25000)
    sync_report.end_compare(50, 50000)

    for i in six.moves.range(25):
        if i % 2 == 0:
            sync_report.print_completion('transferred: %d.txt' % i)
        sync_report.update_transfer(1, 1000)
        time.sleep(0.2)

    sync_report.close()


@six.add_metaclass(ABCMeta)
class AbstractAction(object):
    """
    An action to take, such as uploading, downloading, or deleting
    a file.  Multi-threaded tasks create a sequence of Actions, which
    are then run by a pool of threads.

    An action can depend on other actions completing.  An example of
    this is making sure a CreateBucketAction happens before an
    UploadFileAction.
    """

    def __init__(self, prerequisites):
        """
        :param prerequisites: A list of tasks that must be completed
         before this one can be run.
        """
        self.prerequisites = prerequisites
        self.done = False

    def run(self):
        for prereq in self.prerequisites:
            prereq.wait_until_done()
        self.do_action()
        self.done = True

    def wait_until_done(self):
        # TODO: better implementation
        while not self.done:
            time.sleep(1)

    @abstractmethod
    def do_action(self):
        """
        Performs the action, returning only after the action is completed.

        Will not be called until all prerequisites are satisfied.
        """


class B2UploadAction(AbstractAction):
    def __init__(self, local_full_path, file_name, mod_time):
        self.local_full_path = local_full_path
        self.file_name = file_name
        self.mod_time = mod_time

    def do_action(self):
        raise NotImplementedError()

    def __str__(self):
        return 'b2_upload(%s, %s, %s)' % (self.local_full_path, self.file_name, self.mod_time)


class B2HideAction(AbstractAction):
    def __init__(self, file_name):
        self.file_name = file_name

    def do_action(self):
        raise NotImplementedError

    def __str__(self):
        return 'b2_hide(%s)' % (self.file_name,)


class B2DownloadAction(AbstractAction):
    def __init__(self, file_name, file_id, local_full_path, mod_time_millis):
        self.file_name = file_name
        self.file_id = file_id
        self.local_full_path = local_full_path
        self.mod_time_millis = mod_time_millis

    def do_action(self):
        raise NotImplementedError()

    def __str__(self):
        return (
            'b2_download(%s, %s, %s, %d)' %
            (self.file_name, self.file_id, self.local_full_path, self.mod_time_millis)
        )


class B2DeleteAction(AbstractAction):
    def __init__(self, file_name, file_id):
        self.file_name = file_name
        self.file_id = file_id

    def do_action(self):
        raise NotImplementedError()

    def __str__(self):
        return 'b2_delete(%s, %s)' % (self.file_name, self.file_id)


class LocalDeleteAction(AbstractAction):
    def __init__(self, full_path):
        self.full_path = full_path

    def do_action(self):
        raise NotImplementedError()

    def __str__(self):
        return 'local_delete(%s)' % (self.full_path)


class FileVersion(object):
    """
    Holds information about one version of a file:

       id - The B2 file id, or the local full path name
       mod_time - modification time, in milliseconds, to avoid rounding issues
                  with millisecond times from B2
       action - "hide" or "upload" (never "start")
    """

    def __init__(self, id_, mod_time, action):
        self.id_ = id_
        self.mod_time = mod_time
        self.action = action

    def __repr__(self):
        return 'FileVersion(%s, %s, %s)' % (repr(self.id_), repr(self.mod_time), repr(self.action))


class File(object):
    """
    Holds information about one file in a folder.

    The name is relative to the folder in all cases.

    Files that have multiple versions (which only happens
    in B2, not in local folders) include information about
    all of the versions, most recent first.
    """

    def __init__(self, name, versions):
        self.name = name
        self.versions = versions

    def latest_version(self):
        return self.versions[0]

    def __repr__(self):
        return 'File(%s, [%s])' % (self.name, ', '.join(map(repr, self.versions)))


@six.add_metaclass(ABCMeta)
class AbstractFolder(object):
    """
    Interface to a folder full of files, which might be a B2 bucket,
    a virtual folder in a B2 bucket, or a directory on a local file
    system.

    Files in B2 may have multiple versions, while files in local
    folders have just one.
    """

    @abstractmethod
    def all_files(self):
        """
        Returns an iterator over all of the files in the folder, in
        the order that B2 uses.

        No matter what the folder separator on the local file system
        is, "/" is used in the returned file names.
        """

    @abstractmethod
    def folder_type(self):
        """
        Returns one of:  'b2', 'local'
        """

    @abstractmethod
    def make_full_path(self, file_name):
        """
        Only for local folders, returns the full path to the file.
        """


class LocalFolder(AbstractFolder):
    """
    Folder interface to a directory on the local machine.
    """

    def __init__(self, root):
        """
        Initializes a new folder.

        :param root: Path to the root of the local folder.  Must be unicode.
        """
        if not isinstance(root, six.text_type):
            raise ValueError('folder path should be unicode: %s' % repr(root))
        assert isinstance(root, six.text_type)
        self.root = os.path.abspath(root)

    def folder_type(self):
        return 'local'

    def all_files(self):
        prefix_len = len(self.root) + 1  # include trailing '/' in prefix length
        for relative_path in self._walk_relative_paths(prefix_len, self.root):
            yield self._make_file(relative_path)

    def make_full_path(self, file_name):
        return os.path.join(self.root, file_name.replace('/', os.path.sep))

    def _walk_relative_paths(self, prefix_len, dir_path):
        """
        Yields all of the file names anywhere under this folder, in the
        order they would appear in B2.
        """
        if not isinstance(dir_path, six.text_type):
            raise ValueError('folder path should be unicode: %s' % repr(dir_path))

        # Collect the names
        # We know the dir_path is unicode, which will cause os.listdir() to
        # return unicode paths.
        names = {}  # name to (full_path, relative path)
        dirs = set()  # subset of names that are directories
        for name in os.listdir(dir_path):
            if '/' in name:
                raise Exception(
                    "sync does not support file names that include '/': %s in dir %s" %
                    (name, dir_path)
                )
            full_path = os.path.join(dir_path, name)
            relative_path = full_path[prefix_len:]
            if os.path.isdir(full_path):
                name += u'/'
                dirs.add(name)
            names[name] = (full_path, relative_path)

        # Yield all of the answers
        for name in sorted(names):
            (full_path, relative_path) = names[name]
            if name in dirs:
                for rp in self._walk_relative_paths(prefix_len, full_path):
                    yield rp
            else:
                yield relative_path

    def _make_file(self, relative_path):
        full_path = os.path.join(self.root, relative_path)
        mod_time = int(round(os.path.getmtime(full_path) * 1000))
        slashes_path = u'/'.join(relative_path.split(os.path.sep))
        version = FileVersion(full_path, mod_time, "upload")
        return File(slashes_path, [version])

    def __repr__(self):
        return 'LocalFolder(%s)' % (self.root,)


class B2Folder(AbstractFolder):
    """
    Folder interface to B2.
    """

    def __init__(self, bucket_name, folder_name, api):
        self.bucket_name = bucket_name
        self.folder_name = folder_name
        self.bucket = api.get_bucket_by_name(bucket_name)

    def all_files(self):
        for file_version_info in self.bucket.ls(self.folder_name, show_versions=True):
            mod_time_millis = int(file_version_info.file_info.get(
                'src_modified_millis', file_version_info.upload_timestamp))
            yield FileVersion(file_version_info.id_, mod_time_millis, file_version_info.action)

    def folder_type(self):
        return 'b2'

    def make_full_path(self, file_name):
        raise NotImplementedError()


def next_or_none(iterator):
    """
    Returns the next item from the iterator, or None if there are no more.
    """
    try:
        return six.advance_iterator(iterator)
    except StopIteration:
        return None


def zip_folders(folder_a, folder_b):
    """
    An iterator over all of the files in the union of two folders,
    matching file names.

    Each item is a pair (file_a, file_b) with the corresponding file
    in both folders.  Either file (but not both) will be None if the
    file is in only one folder.
    :param folder_a: A Folder object.
    :param folder_b: A Folder object.
    """
    iter_a = folder_a.all_files()
    iter_b = folder_b.all_files()
    current_a = next_or_none(iter_a)
    current_b = next_or_none(iter_b)
    while current_a is not None or current_b is not None:
        if current_a is None:
            yield (None, current_b)
            current_b = next_or_none(iter_b)
        elif current_b is None:
            yield (current_a, None)
            current_a = next_or_none(iter_a)
        elif current_a.name < current_b.name:
            yield (current_a, None)
            current_a = next_or_none(iter_a)
        elif current_b.name < current_a.name:
            yield (None, current_b)
            current_b = next_or_none(iter_b)
        else:
            assert current_a.name == current_b.name
            yield (current_a, current_b)
            current_a = next_or_none(iter_a)
            current_b = next_or_none(iter_b)


def make_transfer_action(sync_type, source_file, source_folder, dest_folder):
    source_mod_time = source_file.latest_version().mod_time
    if sync_type == 'local-to-b2':
        return B2UploadAction(
            source_folder.make_full_path(source_file.name),
            source_file.name,
            source_mod_time
        )  # yapf: disable
    else:
        return B2DownloadAction(
            source_file.name,
            source_file.latest_version().id_,
            dest_folder.make_full_path(source_file.name),
            source_mod_time
        )  # yapf: disable


def make_file_sync_actions(
    sync_type, source_file, dest_file, source_folder, dest_folder, args, now_millis
):
    """
    Yields the sequence of actions needed to sync the two files
    """
    # Get the modification time of the latest version of the source file
    source_mod_time = 0
    if source_file is not None:
        source_mod_time = source_file.latest_version().mod_time

    # Get the modification time of the latest version of the destination file
    dest_mod_time = 0
    if dest_file is not None:
        dest_mod_time = dest_file.latest_version().mod_time

    # By default, all but the current version at the destination are
    # candidates for cleaning.  This will be overridden in the case
    # where there is no source file.
    dest_versions_to_clean = []
    if dest_file is not None:
        dest_versions_to_clean = dest_file.versions[1:]

    # Case 1: Destination does not exist, or source is newer.
    # All prior versions of the destination file are candidates for
    # cleaning.
    if dest_mod_time < source_mod_time:
        yield make_transfer_action(sync_type, source_file, source_folder, dest_folder)
        if sync_type == 'local-to-b2' and dest_file is not None:
            dest_versions_to_clean = dest_file.versions

    # Case 2: Both exist and source is older
    elif source_mod_time != 0 and source_mod_time < dest_mod_time:
        if args.replaceNewer:
            yield make_transfer_action(sync_type, source_file, source_folder, dest_folder)
        elif args.skipNewer:
            pass
        else:
            raise DestFileNewer('destination file is newer: %s' % (dest_file.name,))

    # Case 3: No source file, but destination file exists
    elif source_mod_time == 0 and dest_mod_time != 0:
        if args.keepDays and sync_type == 'local-to-b2':
            if dest_file.versions[0].action == 'upload':
                yield B2HideAction(dest_file.name)
        # all versions of the destination file are candidates for cleaning
        dest_versions_to_clean = dest_file.versions

    # Clean up old versions
    if sync_type == 'local-to-b2':
        for version in dest_versions_to_clean:
            if args.delete:
                yield B2DeleteAction(dest_file.name, version.id_)
            elif args.keepDays is not None:
                cutoff_time = now_millis - args.keepDays * ONE_DAY_IN_MS
                if version.mod_time < cutoff_time:
                    yield B2DeleteAction(dest_file.name, version.id_)
    elif sync_type == 'b2-to-local':
        for version in dest_versions_to_clean:
            if args.delete:
                yield LocalDeleteAction(version.id_)


def make_folder_sync_actions(source_folder, dest_folder, args, now_millis):
    """
    Yields a sequence of actions that will sync the destination
    folder to the source folder.
    """
    if args.skipNewer and args.replaceNewer:
        raise ValueError('--skipNewer and --replaceNewer are incompatible')

    if args.delete and (args.keepDays is not None):
        raise ValueError('--delete and --keepDays are incompatible')

    source_type = source_folder.folder_type()
    dest_type = dest_folder.folder_type()
    sync_type = '%s-to-%s' % (source_type, dest_type)
    if (source_folder.folder_type(), dest_folder.folder_type()) not in [
        ('b2', 'local'), ('local', 'b2')
    ]:
        raise NotImplementedError("Sync support only local-to-b2 and b2-to-local")
    for (source_file, dest_file) in zip_folders(source_folder, dest_folder):
        for action in make_file_sync_actions(
            sync_type, source_file, dest_file, source_folder, dest_folder, args, now_millis
        ):
            yield action


def parse_sync_folder(folder_name, api):
    """
    Takes either a local path, or a B2 path, and returns a Folder
    object for it.

    B2 paths look like: b2://bucketName/path/name

    Anything else is treated like a local folder.
    """
    if folder_name.startswith('b2://'):
        bucket_and_path = folder_name[5:]
        if '/' not in bucket_and_path:
            bucket_name = bucket_and_path
            folder_name = ''
        else:
            (bucket_name, folder_name) = bucket_and_path.split('/', 1)
        return B2Folder(bucket_name, folder_name, api)
    else:
        return LocalFolder(folder_name)


def count_files(local_folder, reporter):
    """
    Counts all of the files in a local folder.
    """
    for _ in local_folder.all_files():
        reporter.update_local(1)
    reporter.finish_local()


def sync_folders(source_folder, dest_folder, args, now_millis):
    """
    Syncs two folders.  Always ensures that every file in the
    source is also in the destination.  Deletes any file versions
    in the destination older than history_days.
    """

    # Make a reporter to report progress.
    reporter = SyncReport()

    # Make an executor to count files and run all of the actions.
    sync_executor = futures.ThreadPoolExecutor(max_workers=10)

    # First, start the thread that counts the local files.  That's the operation
    # that should be fastest, and it provides scale for the progress reporting.
    local_folder = None
    if source_folder.folder_type() == 'local':
        local_folder = source_folder
    if dest_folder.folder_type() == 'local':
        local_folder = dest_folder
    if local_folder is None:
        raise ValueError('neither folder is a local folder')
    sync_executor.submit(count_files, local_folder, reporter)

    # Schedule each of the actions
    for action in make_folder_sync_actions(source_folder, dest_folder, args, now_millis):
        reporter.print_completion(str(action))

    # Wait for everything to finish
    sync_executor.shutdown()
