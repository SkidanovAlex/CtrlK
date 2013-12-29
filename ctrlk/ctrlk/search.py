import os

# TODO: handle files that are deleted. today we only add and reparse files

# prefixes for the indexDb entries:
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
#   ndef%%%<spelling>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Definitions for symbol navigation
#
#   ndecl%%%<spelling>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Declarations for symbol navigation
#
#   ndefsuf%%%<spelling suffix>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Suffixes of definitions for symbol navigation
#
#   ndeclsuf%%%<spelling suffix>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Suffixes of declarations for symbol navigation
#
#   F%%%<file_name_without_path>%%%<full_file_path> => 1
#      so that we can show files in Ctrl_K
#
#   h%%%<header_name> => <source_file_name>
#      command line args we can use to compile any file
#
# <symbol> is what get_usr for a cursor returns
# <use_type> is a CursorKind.value. If the entry is also a definition, <use_type> is negative of that number
#

REFERENCE_KINDS = dict({
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
103 : 'function call',
501 : 'macro declaraion',
502 : 'macro instantiation'
})

def leveldb_range_iter(conn, starts_with=None):
    if starts_with != None:
        if starts_with[-1] == '%':
            first_excl = starts_with[:-1] + '^'
        else: 
            first_excl = starts_with + "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"
    else:
        first_excl = None
    for key, value in conn.RangeIter(starts_with, first_excl, True):
        yield key, value

def extract_part(line, ordinal):
    return line.split('%%%')[ordinal]

def get_reference_kind(val):
    isDef = False
    if val < 0:
        val = -val
        isDef = True
    if val in REFERENCE_KINDS:
        ret = REFERENCE_KINDS[val]
        if isDef:
            ret = ret.replace("declaration", "DEFINITION")
        return ret
    return "other"

def get_items_matching_pattern(conn, prefix, limit):
    if prefix == "" or prefix == None:
        return ["Search for a function, class, variable, or file name."], []

    ret = []
    locations = []

    ordinal = 0

    for key, value in leveldb_range_iter(conn, 'F%%%' + prefix.lower()):
        if limit > 0:
            full_path = extract_part(key, 2)
            ret.append(os.path.basename(full_path) + " (" + full_path + ") [" + str(ordinal) + "]")
            locations.append([extract_part(key, 2), 1, 1])
            ordinal += 1
            limit -= 1
        else:
            break
    for dbPrefix in ["ndef", "ndefsuf", "ndecl", "ndeclsuf"]:
        for key, value in leveldb_range_iter(conn, dbPrefix + '%%%' + prefix.lower()):
            if limit > 0:
                ret.append(extract_part(key, 6) + " - " + get_reference_kind(int(value)) + " from " + (extract_part(key, 3)) + " [" + str(ordinal) + "]")
                locations.append([extract_part(key, 3), int(extract_part(key, 4)), int(extract_part(key, 5))])
                ordinal += 1
                limit -= 1
            else:
                break

    return ret, locations
