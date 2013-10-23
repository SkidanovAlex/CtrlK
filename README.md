CtrlK
=====

CtrlK is a plugin to navigate C++ symbols. It is based on Clang library, and uses FuzzyFinder as a front-end internally.

It uses compilation databases to get the project files, and LevelDB to store the internal index.

The goal is to implement symbol lookups, GoTo definition and GoTo declaration, as well as some other smaller features.

Currently only symbol lookup (similar to Ctrl+K in QtCreator / Ctrl+Comma in Visual Studio) is implmeneted.

Installation
------------
The easiest way to install CtrlK is to use Vundle.

  ```vim
  Bundle 'L9'
  Bundle 'FuzzyFinder'
  Bundle 'SkidanovAlex/CtrlK'
  ```

If you install it manually instead, make sure that l9 and FuzzyFinder are installed before installing CtrlK

You might also need to install leveldb module for python:

  ```sudo pip install leveldb```

Configuration
-------------
Here's a sample `.vimrc` file:

  ```vim
  let g:ctrlk_clang_library_path="/home/alex/llvm/lib"
  nmap e :call RunCtrlK()<CR>
  nmap E :call GetCtrlKState()<CR>
  ```

Set `g:ctrlk_clang_library_path` to your llvm lib folder, then map the key you would like to use to navigate the symbols to call `RunCtrlK`. In this example I use letter `e`, which default behavior I personally find to be useless in the normal mode. You can also bind some other key to run `GetCtrlKState`, which will print the current state of the parser.

Compilation database
--------------------
CtrlK uses compilation databases to parse your project. If you use CMake, creating the compilation database is just a matter of running:

  ```cmake . -DCMAKE_EXPORT_COMPILE_COMMANDS=ON```

When you run vim, CtrlK will find the compilation database file that is closest to the current folder, and place its own index next to it.

