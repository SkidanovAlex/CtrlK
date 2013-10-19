import leveldb
import json
import os.path

from clang.cindex import Index, Config, TranslationUnitLoadError

# TODO: handle files that are deleted. today we only add and reparse files

# prefixes for the leveldb entries:
#
#   f%%%<file_name> => <lastModified>
#      file <file_name> was indexed, at that moment its mtime was lastModified
#
#   c%%%<file_name>%%%<symbol> => 1
#      file <file_name> contains symbol <symbol>. used to delete symbols when we reparse file
#
#   spelling%%%<symbol> => <spelling>
#      spelling of a symbol
#
#   s%%%<symbol>%%%<file_name>%%%<line>%%%<col> => <use_type>
#      actual symbols database for 'goto definition' and 'goto declaration'
#
#   n%%%<spelling>%%%<file_name>%%%<line>%%%<col> => <use_type>
#      actual symbols database for Ctrl+K
#
#   F%%%<file_name_without_path>%%%<full_file_path> => 1
#      so that we can show files in Ctrl_K
#
#   h%%%<file_name>%%%<header_name> => <command_line_args>
#      file <file_name> includes file <header_name>. used to both traverse header files and so
#      that we can delete them when we reparse the <file_name>
#      the value is command arguments with which the source file was compiled. same arguments will
#      be used to parse the header file
#
# <symbol> is what get_usr for a cursor returns
#

def ResetIndex(indexDbPath):
    indexDb = leveldb.LevelDB(indexDbPath)
    for key in indexDb.RangeIter():
        indexDb.Delete(key[0])

def DeleteFromIndex(indexDb, pattern, callback = None):
    assert pattern[-1] == '%'
    patternTo = pattern[:-1] + '^'
#    print patternTo
    for key in indexDb.RangeIter(pattern, patternTo):
        print "DELETING " + key[0]
        indexDb.Delete(key[0])
        if callback != None:
            callback(indexDb, key[0])

def IterateOverIndex(indexDb, fromStr, toStr):
    for kv in indexDb.RangeIter(fromStr, toStr, True):
        yield kv

def ExtractPart(line, ordinal):
    return line.split('%%%')[ordinal]

def IterateOverFiles(compDb, indexDb):
    for entry in compDb:
        if 'command' in entry and 'file' in entry:
            command = entry['command'].split()
            if '++' in command[0]:
                fileName = entry['file']
                flags = command[1:]

                lastModified = int(os.path.getmtime(fileName))
                print fileName
                yield (fileName, command, lastModified)

def ExtractSymbols(indexDb, fileName, node):
    if node.location.file != None and node.location.file.name != fileName:
        return

    symbol = node.get_usr()
    if symbol:
#        print node.get_usr(), '=>', node.spelling, ' (', node.location.line, 'x', node.location.column, ')'
        indexDb.Put("spelling%%%" + symbol, node.spelling)
        indexDb.Put("c%%%" + fileName + "%%%" + symbol, '1')
        indexDb.Put("s%%%" + symbol + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(node.kind))
        indexDb.Put("n%%%" + node.spelling + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(node.kind))
    for c in node.get_children():
        ExtractSymbols(indexDb, fileName, c)

def GetSymbolSpelling(indexDb, symbol):
    try:
        return indexDb.Get("spelling%%%" + symbol)
    except KeyError:
        return "(not found)"

def RemoveSymbol(indexDb, key):
    symbol = ExtractPart(key, 2)
    fname = ExtractPart(key, 1)

    DeleteFromIndex(indexDb, "s%%%" + symbol + "%%%" + fname + "%%%")
    DeleteFromIndex(indexDb, "n%%%" + GetSymbolSpelling(indexDb, symbol) + "%%%" + fname + "%%%")
    

def ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders):
    try:
        lastKnown = int(indexDb.Get('f%%%' + fileName))
    except KeyError:
        lastKnown = 0

    if lastKnown < lastModified:
        indexDb.Put('F%%%' + os.path.basename(fileName) + "%%%" + fileName, '1')
        try:
            tu = index.parse(None, command + ["-I%s" % x for x in additionalInclude])
        except TranslationUnitLoadError as e:
            # TODO: handle failure
            return

        # set it to zero so that if we crash or stop while parse, it is reparsed when we are restarted
        indexDb.Put('f%%%' + fileName, str(0))

        for x in tu.diagnostics:
            if x.severity >= 3:
                # TODO: remember errors
                pass

        # parse headers
        DeleteFromIndex(indexDb, "h%%%" + fileName + "%%%")
        includes = set()
        for incl in tu.get_includes():
            if incl.include:
                includes.add(incl.include)

        for incl in includes:
            indexDb.Put("h%%%" + fileName + "%%%" + incl.name, ' '.join(command))

        # parse symbols
        DeleteFromIndex(indexDb, "c%%%" + fileName + "%%%", RemoveSymbol)
        ExtractSymbols(indexDb, fileName, tu.cursor)

        indexDb.Put('f%%%' + fileName, str(lastModified))

def UpdateCppIndex(indexDbPath, compilationDbPath, additionalInclude):
    indexDb = leveldb.LevelDB(indexDbPath)

    with file(compilationDbPath) as f:
        compDb = json.loads(f.read())

        index = Index.create()

        # on the first scan we parse both headers and symbols
        for fileName, command, lastModified in IterateOverFiles(compDb, indexDb):
            ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders = True)

        # add all the header files to the compilation database we use
        for key, value in IterateOverIndex(indexDb, "h%%%", "h%%^"):
            compDb.append({'command': value, 'file': ExtractPart(key, 2)})

        # on the second scan we only parse symbols (it should only parse the headers added after the first scan)
        for fileName, command, lastModified in IterateOverFiles(compDb, indexDb):
            ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders = False)

Config.set_library_path("/home/alex/llvm/lib")
ResetIndex('./db')
UpdateCppIndex('./db', '/home/alex/mysql/compile_commands.json', ["/home/alex/llvm/lib/clang/3.2/include/"])
