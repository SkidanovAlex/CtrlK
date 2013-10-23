CtrlK
=====

This repository is work in progress. Plugin is not usable yet.

CtrlK is a plugin to navigate C++ symbols. It is based on Clang library, and uses FuzzyFinder as a front-end internally.

It uses compilation databases to get the project files, and LevelDB to store the internal index.

The goal is to implement symbol lookups, GoTo definition and GoTo declaration, as well as some other smaller features.


