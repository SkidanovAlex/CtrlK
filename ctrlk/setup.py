from distutils.core import setup, Extension

# Disable CXX environment variable if it starts with ccache
import os
cxx_var = os.getenv("CXX")
if cxx_var and 'ccache' in cxx_var:
    os.environ["CXX"] = "g++"

module1 = Extension(
                   "ctrlk.indexer",
                   sources = ["ctrlk/indexer.cpp"],
                   extra_objects = ["./py-leveldb/leveldb/libleveldb.a", "./py-leveldb/snappy-read-only/.libs/libsnappy.a"],
                   language="c++",
                   extra_compile_args=["-std=c++0x", "-O3", "-g", "-Werror"],
                   include_dirs=['.', './py-leveldb/leveldb/include'],
                   libraries=['clang', 'pthread'],
                   define_macros=[])

setup (name = 'ctrlk',
       version = '1.0',
       description = 'Source code parsing library powering CtrlK',
       author = 'Alex Skidanov, Ankur Goyal',
       author_email = 'alex@memsql.com, ankur@memsql.com',
       url = '',
       long_description ='', 
       ext_modules = [module1],
       packages=['ctrlk'])
