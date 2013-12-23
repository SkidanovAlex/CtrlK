#!/bin/bash

shopt -s expand_aliases
NCORES=`grep -c ^processor /proc/cpuinfo`
MAKE_CORES=`expr $NCORES + 1`
alias make="make -j $MAKE_CORES"

svn checkout http://py-leveldb.googlecode.com/svn/trunk/ py-leveldb
cd py-leveldb

./compile_leveldb.sh
