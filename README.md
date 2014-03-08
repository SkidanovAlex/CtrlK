CtrlK
=====

CtrlK is a plugin to navigate C++ symbols. It is based on Clang library, and uses FuzzyFinder as a front-end internally.

It uses compilation databases to get the project files, and LevelDB to store the internal index.

Supported features are:

1. Symbol navigation similar to Ctrl+K in QtCreator / Ctrl+Comma in Visual Studio

2. GoTo Definition 

3. Find References

Installation
------------
Before installing the ctrlk plugin, you need to install ctrlk python library, as well as leveldb and clang libraries:

  ```bash
  sudo pip install leveldb
  sudo pip install clang
  sudo pip install ctrlk
  ```

The easiest way to install CtrlK plugin itself is to use Vundle.

  ```vim
  Bundle 'L9'
  Bundle 'FuzzyFinder'
  Bundle 'SkidanovAlex/CtrlK'
  ```

Configuration
-------------
Here's a sample `.vimrc` file, that mimics QtCreator key bindings:

  ```vim
  let g:ctrlk_clang_library_path="/home/user/llvm/lib"
  nmap <F3> :call GetCtrlKState()<CR>
  nmap <C-k> :call CtrlKNavigateSymbols()<CR>
  nmap <F2> :call CtrlKGoToDefinition()<CR>
  nmap <F12> :call CtrlKGetReferences()<CR>
  ```

Set `g:ctrlk_clang_library_path` to your llvm lib folder (the folder that contains `libclang.so`).
This maps Ctrl+k to open symbol navigation window, F2 to go to the current symbol's definition and F12 to show all the references to symbol under cursor.
F3 is showing the current state of the indexer and background parsing thread in the form

  ```Index: <status of the indexer> / Current: <status of the parsing thread>```

Note that CtrlK's indexer is rather slow. Indexing your project first time can take a considerable amount of time. Pressing F3 will tell you which file is being parsed right now. When all the files are parsed, F3 will print "Sleeping" as the indexer status.

With the bindings above F2 opens the definition in the same window. If you want to open the definition in a new window after a split or a vsplit, use the following bindings:

  ```vim
  nmap <F4> :call CtrlKGoToDefinitionWithSplit('j')<CR>
  nmap <F5> :call CtrlKGoToDefinitionWithSplit('k')<CR>
  ```

Compilation database
--------------------
CtrlK uses compilation databases to parse your project. If you use CMake, creating the compilation database is just a matter of running:

  ```cmake . -DCMAKE_EXPORT_COMPILE_COMMANDS=ON```

When you run vim, CtrlK will find the compilation database file that is closest to the current folder, and place its own index next to it into a `.ctrlk` folder.

