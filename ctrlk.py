import leveldb
import json
import os.path
#import vim

from clang.cindex import Index, Config, TranslationUnitLoadError, CursorKind

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
# <use_type> is a CursorKind.value. If the entry is also a definition, <use_type> is negative of that number
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
                fileName = os.path.normpath(entry['file'])

                # it should be startswith in the general case, but for MemSQL we can have /usr/include in the middle of the string
                if "/usr/include" in fileName:
                    continue
                flags = command[1:]

                lastModified = int(os.path.getmtime(fileName))
                yield (fileName, command, lastModified)

def GetNodeUseType(node):
    ret = node.kind.value
    if node.is_definition():
        ret = -ret
    return ret

def ExtractSymbols(batch, fileName, node):
    if node.location.file != None and os.path.normpath(node.location.file.name) != os.path.normpath(fileName):
        return

    symbol = node.get_usr()
    if symbol:
#        print node.get_usr(), '=>', node.spelling, ' (', node.location.line, 'x', node.location.column, ')'
        batch.Put("spelling%%%" + symbol, node.spelling)
        batch.Put("c%%%" + fileName + "%%%" + symbol, '1')
        batch.Put("s%%%" + symbol + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(GetNodeUseType(node)))
        batch.Put("n%%%" + node.spelling + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(GetNodeUseType(node)))
    for c in node.get_children():
        ExtractSymbols(batch, fileName, c)

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
        print fileName
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
                includes.add(os.path.normpath(incl.include.name))

        for incl in includes:
            indexDb.Put("h%%%" + fileName + "%%%" + incl, ' '.join(command))

        # parse symbols
        DeleteFromIndex(indexDb, "c%%%" + fileName + "%%%", RemoveSymbol)

        batch = leveldb.WriteBatch()
        ExtractSymbols(batch, fileName, tu.cursor)
        indexDb.Write(batch)

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

lastLocations = []
lastRet = []

def JumpTo(filename, line, column):
  if filename != vim.current.buffer.name:
    try:
      vim.command("edit %s" % filename)
    except:
      # For some unknown reason, whenever an exception occurs in
      # vim.command, vim goes crazy and output tons of useless python
      # errors, catch those.
      return
  else:
    vim.command("normal m'")
  vim.current.window.cursor = (line, column - 1)

def NavigateToEntry(entry):
    global lastLocations
    if '[' in entry:
        id = int(entry[entry.find('[') + 1:entry.find(']')])
        loc = lastLocations[id]
        JumpTo(loc[0], int(loc[1]), int(loc[2]))

def GetItemsMatchingPattern(prefix, limit):
    global lastLocations
    global lastRet

    ret = []
    locations = []
    print prefix

    ordinal = 0
    try:
        indexDb = leveldb.LevelDB("/home/alex/CtrlK/db")

        for key in indexDb.RangeIter('n%%%'+prefix, 'n%%%' + prefix + '~~~~~~~~~~~~~~~', True):
            if limit > 0:
                ret.append(ExtractPart(key[0], 1) + " - " + GetReferenceKind(int(key[1])) + " from " + (ExtractPart(key[0], 2)) + " [" + str(ordinal) + "]")
                locations.append([ExtractPart(key[0], 2), int(ExtractPart(key[0], 3)), int(ExtractPart(key[0], 4))])
                ordinal += 1
                limit -= 1
        for key in indexDb.RangeIter('F%%%'+prefix, 'F%%%' + prefix + '~~~~~~~~~~~~~~~'):
            if limit > 0:
                ret.append(ExtractPart(key[0], 1) + " [" + str(ordinal) + "]")
                locations.append([ExtractPart(key[0], 2), 1, 1])
                ordinal += 1
                limit -= 1
        lastRet = ret
        lastLocations = locations

        return ret
    except leveldb.LevelDBError as e:
        return lastRet

if __name__ == "__main__":
#    Config.set_library_path("/home/alex/llvm/lib")
#    ResetIndex('./db')
#    UpdateCppIndex('./db', '/home/alex/mysql/compile_commands.json', ["/home/alex/llvm/lib/clang/3.2/include/"])

    pass

def GetReferenceKind(val):
    isDef = False
    if val < 0:
        val = -val
        isDef = True
    if val in referenceKinds:
        ret = referenceKinds[val]
        if isDef:
            ret = ret.replace("declaration", "DEFINITION")
        return ret
    return "other"

referenceKinds = dict({
 1 : 'type declaration',
 2 : 'type declaration',
 3 : 'type declaration',
 4 : 'type declaration',
 5 : 'type declaration',
 6 : 'member declaration',
 7 : 'enum declaration',
 8 : 'function declaration',
 9 : 'variable declaration',
10 : 'argument declaration',
20 : 'typedef declaration',
21 : 'method declaration',
22 : 'namespace declaration',
24 : 'constructor declaration',
25 : 'destructor declaration',
26 : 'conversion function declaration',
27 : 'template type parameter',
28 : 'non-type template parameter',
29 : 'template template parameter',
30 : 'function template declaration',
31 : 'class template declaration',
32 : 'class template partial specialization',
33 : 'namespace alias',
43 : 'type reference',
44 : 'base specifier',
45 : 'template reference',
46 : 'namespace reference',
47 : 'member reference',
48 : 'label reference',
49 : 'overloaded declaration reference',
100 : 'expression',
101 : 'reference',
102 : 'member reference',
103 : 'function call'
})

