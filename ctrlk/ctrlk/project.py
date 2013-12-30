from clang.cindex import Config, TranslationUnitLoadError, Index
import leveldb
import multiprocessing
import os

from ctrlk import indexer
from ctrlk import search

try:
    import simplejson as json
except ImportError:
    import json

class Project(object):
    def __init__(self, library_path, project_root, n_workers=None):
        if n_workers is None:
            n_workers = (multiprocessing.cpu_count() * 3) / 2

        self.clang_library_path = library_path

        if not Config.loaded:
            Config.set_library_path(self.clang_library_path)
            Config.set_compatibility_check(False)

        self.builtin_header_path = getBuiltinHeaderPath(self.clang_library_path)

        if self.builtin_header_path is None:
            raise Exception("Cannot find clang includes")

        project_root = os.path.abspath(project_root)

        curr_path = project_root
        self.compile_commands_path = None
        while curr_path:
            compile_commands_path = os.path.join(curr_path, 'compile_commands.json')
            if os.path.exists(compile_commands_path):
                self.compile_commands_path = compile_commands_path
                self.index_db_path = os.path.join(curr_path, '.ctrlk-index')
                self.project_root = curr_path
                break
            elif curr_path == '/':
                break
            curr_path = os.path.dirname(curr_path)

        if self.compile_commands_path is None:
            raise Exception("Could not find a 'compile_commands.json' file in the " +\
                                "directory hierarchy from '%s'" % (project_root))

        self._compilation_db = None
        self._compilation_db_modtime = 0

        self._leveldb_connection = None
        indexer.start(self.leveldb_connection, n_workers)

    @property
    def leveldb_connection(self):
        if not self._leveldb_connection:
            self._leveldb_connection = leveldb.LevelDB(self.index_db_path)
        return self._leveldb_connection

    @property
    def compilation_db(self):
        if self._compilation_db is None \
                or get_file_modtime(self.compile_commands_path) > self._compilation_db_modtime:
            with open(self.compile_commands_path, 'r') as f:
                raw = json.load(f)
            self._compilation_db = {}
            for entry in raw:
                if 'command' in entry and 'file' in entry:
                    command = entry['command'].split()
                    if '++' in command[0] or "cc" in command[0] or "clang" in command[0]:
                        file_name = os.path.abspath(entry['file'])

                        # it could be startswith in the general case, but for my 
                        # specific purposes I needed to check the middle of the string too -- AS
                        if "/usr/include" in file_name:
                            continue

                        if not os.path.exists(file_name):
                            continue

                        self._compilation_db[file_name] = command + ["-I" + self.builtin_header_path]
        return self._compilation_db

    def get_file_args(self, file_name):
        mod_time = get_file_modtime(file_name)
        compile_command = None
        if file_name in self.compilation_db:
            origin_file = file_name
            compile_command = self.compilation_db[file_name]
        else:
            try:
                origin_file = self.leveldb_connection.Get("h%%%" + file_name)
            except KeyError:        
                return None, None, None
            compile_command = self.compilation_db[origin_file]

        return origin_file, compile_command, mod_time

    def parse_file(self, file_name):
        origin_file, compile_command, mod_time = self.get_file_args(file_name)
        indexer.add_file_to_parse(origin_file, compile_command, mod_time)

    def scan_and_index(self):
        project_files = self.compilation_db
        for file_name, compile_command in project_files.items():
            try:
                mod_time = get_file_modtime(file_name)
            except OSError:
                continue
            indexer.add_file_to_parse(file_name, compile_command, mod_time)

        cpp_files_to_reparse = set()
        for header_file_key, origin_file_name in search.leveldb_range_iter(self.leveldb_connection, "h%%%"):
            header_file_name = search.extract_part(header_file_key, 1)
            saved_mod_time = int(self.leveldb_connection.Get("f%%%" + header_file_name))

            try:
                real_mod_time = get_file_modtime(header_file_name)
            except OSError:
                indexer.remove_file_symbols(header_file_name)
                continue

            if real_mod_time <= saved_mod_time:
                continue

            compile_command = project_files[origin_file_name]
            if origin_file_name not in cpp_files_to_reparse:
                cpp_files_to_reparse.add(origin_file_name)
                indexer.add_file_to_parse(origin_file_name, compile_command, real_mod_time)

    def wait_on_work(self):
        indexer.wait_on_work()

    def work_queue_size(self):
        return indexer.work_queue_size()

def get_file_modtime(file_name):
    return int(os.path.getmtime(file_name))

# the following two functions are taken from clang_complete plugin
def canFindBuiltinHeaders(index, args = []):
  flags = 0
  currentFile = ("test.c", '#include "stddef.h"')
  try:
    tu = index.parse("test.c", args, [currentFile], flags)
  except TranslationUnitLoadError:
    return 0
  return len(tu.diagnostics) == 0

# Derive path to clang builtin headers.
#
# This function tries to derive a path to clang's builtin header files. We are
# just guessing, but the guess is very educated. In fact, we should be right
# for all manual installations (the ones where the builtin header path problem
# is very common) as well as a set of very common distributions.
def getBuiltinHeaderPath(library_path):
  index = Index.create()
  knownPaths = [
          library_path + "/../lib/clang", # default value
          library_path + "/../clang", # gentoo
          library_path + "/clang", # opensuse
          library_path + "/", # Google
          "/usr/lib64/clang", # x86_64 (openSUSE, Fedora)
          "/usr/lib/clang"
  ]

  for path in knownPaths:
    try:
      files = os.listdir(path)
      if len(files) >= 1:
        files = sorted(files)
        subDir = files[-1]
      else:
        subDir = '.'
      path = path + "/" + subDir + "/include/"
      arg = "-I" + path
      if canFindBuiltinHeaders(index, [arg]):
        return path
    except Exception:
      pass

  return None
