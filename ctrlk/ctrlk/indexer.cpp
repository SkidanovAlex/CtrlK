// $CXX -I/usr/local/include -fPIC -lclang -lpthread -shared -Wl,-soname,libclang_index.so.1 -o libclang_index.so.1.0 test.cpp
// g++ --std=c++0x -I/usr/local/include -fPIC -lclang -lpthread -lleveldb -shared -Wl,-soname,libclang_index.so.1 -o libclang_index.so.1.0 test.cpp
//

#include <Python.h>

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <sys/time.h>
#include <string.h>
#include <unistd.h>
#include <sstream>
#include <algorithm>

#include <vector>
#include <queue>
#include <thread>
#include <set>

#include <clang-c/CXCompilationDatabase.h>
#include <clang-c/Index.h>

#include <pthread.h>
#include <leveldb/db.h>
#include <leveldb/write_batch.h>

#include "py-leveldb/leveldb_ext.h"

#define TOKEN_PASTEx(x, y) x ## y
#define TOKEN_PASTE(x, y) TOKEN_PASTEx(x, y)

// We were using std::function instead of a template here before, storing it by value
//    instead of storing it by reference. However, with that approach it was allocating
//    memory on the heap while copying lambda into std::function.
//

template <class T>
class AutoCallOnOutOfScope
{
public:
    AutoCallOnOutOfScope(T& destructor) : m_destructor(destructor) { }
    ~AutoCallOnOutOfScope() { m_destructor(); }
private:
    T& m_destructor;
};

#define Auto_INTERNAL(Destructor, counter) \
    auto TOKEN_PASTE(Auto_func_, counter) = [&]() { Destructor; }; \
    AutoCallOnOutOfScope<decltype(TOKEN_PASTE(Auto_func_, counter))> TOKEN_PASTE(Auto_instance_, counter)(TOKEN_PASTE(Auto_func_, counter));

#define Auto(Destructor) Auto_INTERNAL(Destructor, __COUNTER__)

std::string BaseName(std::string fileName)
{
    size_t lastSlash = fileName.rfind("/");
    if (lastSlash + 1 < fileName.size())
    {
        return std::string(fileName, lastSlash + 1);
    }
    else
    {
        return std::string(fileName);
    }
}

std::string ExtractPart(std::string s, int ordinal)
{
    std::string delimiter = "%%%";

    size_t pos = 0;
    int i = 0;
    size_t nextpos = 0;
    while ((nextpos = s.find(delimiter, pos)) != std::string::npos)
    {
        if (i == ordinal)
        {
            return s.substr(pos, nextpos-pos);
        }

        i++;
        pos = nextpos + delimiter.length();
    }

    if (i == ordinal)
    {
        return s.substr(pos, nextpos-pos);
    }

    assert(false);
    return std::string("");
}

leveldb::DB* db;
std::vector<std::thread> g_workers;

struct CompileCommand
{
    CompileCommand(const char* arg_fileName, const std::vector<std::string>& arg_args, time_t arg_modTime)
    {
        fileName = strdup(arg_fileName);
        nargs = arg_args.size();

        args = new char*[nargs];
        for (int i = 0; i < nargs; i++)
        {
            args[i] = strdup(arg_args[i].c_str());
        }

        modTime = arg_modTime;
    }

    void Clear()
    {
        delete fileName;
        for (int i = 0; i < nargs; i++)
        {
            delete args[i];
        }
        delete[] args;
        args = nullptr;
    }

    char* fileName;
    char** args;
    int nargs;
    time_t modTime;
};

std::queue<CompileCommand> work;
int g_outstandingTasks = 0;
typedef std::set<std::string> AllowedFiles_t;

pthread_mutex_t g_worklock = PTHREAD_MUTEX_INITIALIZER;
pthread_cond_t g_workcond = PTHREAD_COND_INITIALIZER;
pthread_cond_t g_finished_cond = PTHREAD_COND_INITIALIZER;

std::string ExtractString(CXString clangString)
{
    const char* cstr = clang_getCString(clangString);
    std::string ret(cstr ? cstr : "");
    clang_disposeString(clangString);
    return ret;
}

std::string NormPath(std::string fileName)
{
    char* absoluteFileName = realpath(fileName.c_str(), nullptr);
    if (absoluteFileName == nullptr)
    {
        assert(false);
        return fileName;
    }
    std::string ret(absoluteFileName);
    free(absoluteFileName);
    return ret;
}

std::string GetSpelling(CXCursor x)
{
    if (!clang_isDeclaration(clang_getCursorKind(x)))
    {
        return std::string("");
    }
    else
    {
        return ExtractString(clang_getCursorSpelling(x));
    }
}

std::string DbEntryPrefix(int pos, const char* nodeS)
{
    std::string ret = std::string("n") + std::string(nodeS);

    if (pos != 0)
    {
        ret += std::string("suf");
    }

    return ret;
}

std::string DbEntryPrefix(int pos, CXCursor node)
{
    if (clang_isCursorDefinition(node))
    {
        return DbEntryPrefix(pos, "def");
    }
    else
    {
        return DbEntryPrefix(pos, "decl");
    }
}

time_t GetFileModificationTime(const char* fileName)
{
    struct stat info;
    stat(fileName, &info);
    return info.st_mtime;
}

bool NeedToParseFile(std::string fileName, time_t& actualModTime, time_t& savedModTime)
{
    if (actualModTime == 0)
    {
        actualModTime = GetFileModificationTime(fileName.c_str());
    }
    savedModTime = 0;

    std::string modTime;
    leveldb::Status status = db->Get(leveldb::ReadOptions(), std::string("f%%%") + fileName, &modTime);

    if (status.ok())
    {
        savedModTime = strtol(modTime.c_str(), nullptr, 10);
    }

    return actualModTime > savedModTime;
}

void SaveParsedFile(std::string fileName, time_t modTime)
{
    char buf[100];
    snprintf(buf, sizeof(buf), "%ld", modTime);
    db->Put(leveldb::WriteOptions(), std::string("f%%%") + fileName, std::string(buf));
    db->Put(leveldb::WriteOptions(), std::string("F%%%") + BaseName(fileName)
            + std::string("%%%") + fileName, std::string("1"));
}

struct IncludedFileContext
{
    std::string originFile;
    AllowedFiles_t allowedFiles;
};

void IncludedFileVisitor(CXFile includedFile, CXSourceLocation* inclusionStack, uint32_t includeLen, CXClientData data)
{
    std::string relativeFileName = ExtractString(clang_getFileName(includedFile));
    std::string fileName = NormPath(relativeFileName);

    time_t actualModTime = GetFileModificationTime(fileName.c_str());

    IncludedFileContext* ctx = reinterpret_cast<IncludedFileContext*>(data); 
    if (fileName == ctx->originFile)
    {
        return;
    }

    std::string modTime;
    time_t savedModTime = 0;

    if (!NeedToParseFile(fileName, actualModTime, savedModTime))
    {
        return;
    }

    pthread_mutex_lock(&g_worklock);
    Auto(pthread_mutex_unlock(&g_worklock));

    // Repeat the same check, but with the lock
    //
    if (!NeedToParseFile(fileName, actualModTime, savedModTime))
    {
        return;
    }

    ctx->allowedFiles.insert(fileName);

    SaveParsedFile(fileName, actualModTime);
    db->Put(leveldb::WriteOptions(), std::string("h%%%") + fileName, ctx->originFile);
}

CXChildVisitResult SymbolVisitor(CXCursor cursor, CXCursor parent, CXClientData data)
{
    CXSourceLocation source = clang_getCursorLocation(cursor);
    CXFile cxfile;
    uint32_t lineNumber = 0;
    uint32_t columnNumber = 0;
    clang_getExpansionLocation(source, &cxfile, &lineNumber, &columnNumber, nullptr);
    std::string relativeFileName = ExtractString(clang_getFileName(cxfile));

    if (relativeFileName.empty())
    {
        return CXChildVisit_Recurse;
    }

    std::string fileName = NormPath(relativeFileName);

    AllowedFiles_t* allowedFiles = reinterpret_cast<AllowedFiles_t*>(data);
    bool foundMatch = false;
    for (std::string actualFileName : *allowedFiles)
    {
        if (strcmp(fileName.c_str(), actualFileName.c_str()) == 0)
        {
            foundMatch = true;
            break;
        }
    }

    if (!foundMatch)
    {
        return CXChildVisit_Continue;
    }

    int kind = (int) clang_getCursorKind(cursor);
    if (clang_isCursorDefinition(cursor))
    {
        kind = -kind;
    }
    char kindBuf[32];
    snprintf(kindBuf, sizeof(kindBuf), "%d", kind);

    std::string symbol = ExtractString(clang_getCursorUSR(cursor));
    std::string spelling = GetSpelling(cursor);
    std::string displayName = ExtractString(clang_getCursorDisplayName(cursor));

    if (!symbol.empty() && spelling.empty())
    {
        spelling = displayName;
    }

    bool addToN = true;

    CXCursor reference = clang_getCursorReferenced(cursor);
    if (!clang_Cursor_isNull(reference))
    {
        if (symbol.empty())
        {
            symbol = ExtractString(clang_getCursorUSR(reference));
            addToN = false;
        }
        if (spelling.empty())
        {
            spelling = GetSpelling(reference);
        }
        if (!symbol.empty() && spelling.empty())
        {
            spelling = ExtractString(clang_getCursorDisplayName(reference));
        }
    }

    if (!symbol.empty() && !spelling.empty())
    {
        leveldb::WriteBatch batch;
        batch.Put(std::string("spelling%%%") + symbol, spelling);

        std::string key = std::string("c%%%") + fileName + std::string("%%%") + symbol;
        batch.Put(key, std::string("1"));

        std::stringstream locationString;
        locationString << "s%%%" << symbol << "%%%" << fileName << "%%%" << lineNumber << "%%%" << columnNumber;
        batch.Put(locationString.str(), std::string(kindBuf));

        if (addToN)
        {
            CXCursor parent = cursor;
            while (!clang_Cursor_isNull(parent = clang_getCursorSemanticParent(parent)))
            {
                std::string parentSpelling = GetSpelling(parent);
                if (!parentSpelling.empty())
                {
                    displayName = parentSpelling + std::string("::") + displayName;
                }
            }

            for (size_t i = 0; i < spelling.size(); i++)
            {
                std::string suffix = std::string(spelling, i, spelling.size());
                std::transform(suffix.begin(), suffix.end(), suffix.begin(), ::tolower);

                std::stringstream suffixStream;
                suffixStream << DbEntryPrefix(i, cursor) << "%%%" << suffix << "%%%" << symbol << "%%%" << fileName 
                                << "%%%" << lineNumber << "%%%" << columnNumber << "%%%" << displayName;
                batch.Put(suffixStream.str(), std::string(kindBuf));
            }
        }

        db->Write(leveldb::WriteOptions(), &batch);
    }

    return CXChildVisit_Recurse;
}

void DeleteFromIndex(std::string prefix, leveldb::WriteBatch* batch, 
        const std::function<void (std::string, leveldb::WriteBatch*)> callback)
{
    std::string rangeStart = prefix + std::string("%%%");
    std::string rangeEnd = prefix + std::string("%%^");
    leveldb::Iterator* iter = db->NewIterator(leveldb::ReadOptions());
    leveldb::Slice start(rangeStart);
    leveldb::Slice end(rangeEnd);

    iter->Seek(start);
    while(iter->Valid())
    {
        leveldb::Slice key = iter->key();
        if (key.compare(end) >= 0)
        {
            break;
        }

        std::string keyS(key.data(), 0, key.size());
        batch->Delete(key);
        callback(keyS, batch);
        iter->Next();
    }

    delete iter;
}

void EmptyDeleteCallback(std::string, leveldb::WriteBatch*) { }

void DeleteFromIndex(std::string prefix, leveldb::WriteBatch* batch)
{
    DeleteFromIndex(prefix, batch, EmptyDeleteCallback);
}

std::string GetSymbolSpelling(std::string spelling)
{
    std::string ret;
    leveldb::Status status = db->Get(leveldb::ReadOptions(), std::string("spelling%%%") + spelling, &ret);
    if (status.ok())
    {
        return ret;
    }
    else
    {
        return std::string("(not found)");
    }
}

void RemoveSymbol(std::string symbolKey, leveldb::WriteBatch* batch)
{
    std::string fname = ExtractPart(symbolKey, 1);
    std::string symbol = ExtractPart(symbolKey, 2);
    std::string spelling = GetSymbolSpelling(symbol);

    DeleteFromIndex(std::string("s%%%") + symbol + std::string("%%%") + fname , batch);

    // UNDONE: refactor this so it's shared with SymbolVisitor
    //
    for (const char* symbolType : {"def", "decl"})
    {
        for (size_t i = 0; i < spelling.size(); i++)
        {
            std::string suffix = std::string(spelling, i, spelling.size());
            std::transform(suffix.begin(), suffix.end(), suffix.begin(), ::tolower);

            std::string entryPrefix = DbEntryPrefix(i, symbolType) + "%%%" + suffix + "%%%" + symbol + "%%%" + fname;
            DeleteFromIndex(entryPrefix, batch);
        }
    }
}

void RemoveFileSymbols(std::string fileName)
{
    leveldb::WriteBatch batch;
    DeleteFromIndex(std::string("c%%%") + fileName, &batch, RemoveSymbol);
    db->Write(leveldb::WriteOptions(), &batch);
}

void worker()
{
    while (true)
    {
        pthread_mutex_lock(&g_worklock);
        while (work.empty())
        {
            pthread_cond_wait(&g_workcond, &g_worklock);
        }

        CompileCommand command = work.front();
        work.pop();
        pthread_mutex_unlock(&g_worklock);

        std::string fileNameStr(command.fileName);

        time_t actualModTime = command.modTime;
        time_t savedModTime = 0;
        if (NeedToParseFile(fileNameStr, actualModTime, savedModTime))
        {
            auto idx = clang_createIndex(0, 0);

            struct timeval start, end;

//            long seconds, useconds;    

            gettimeofday(&start, NULL);

            CXTranslationUnit tu = clang_parseTranslationUnit(idx, nullptr, command.args, command.nargs, nullptr, 0, CXTranslationUnit_DetailedPreprocessingRecord);
            gettimeofday(&end, NULL);

//            seconds  = end.tv_sec  - start.tv_sec;
//            useconds = end.tv_usec - start.tv_usec;
//            long parseTime = ((seconds) * 1000 + useconds/1000.0) + 0.5;

            IncludedFileContext ctx;
            ctx.originFile = fileNameStr;
            ctx.allowedFiles.insert(fileNameStr);
            clang_getInclusions(tu, IncludedFileVisitor, reinterpret_cast<CXClientData>(&ctx));

            for (std::string allowedFile : ctx.allowedFiles)
            {
                // UNDONE: make this in the same batch as the extract, so that we atomically have the new symbols
                //
                RemoveFileSymbols(allowedFile);
            }

            gettimeofday(&start, NULL);
            clang_visitChildren(clang_getTranslationUnitCursor(tu), SymbolVisitor, reinterpret_cast<CXClientData>(&ctx.allowedFiles));
            gettimeofday(&end, NULL);

//            seconds  = end.tv_sec  - start.tv_sec;
//            useconds = end.tv_usec - start.tv_usec;
//            long extractTime = ((seconds) * 1000 + useconds/1000.0) + 0.5;

//            fprintf(stderr, "%s : parsing = %ld ms, extracting = %ld ms\n", command.fileName, parseTime, extractTime);
//            fprintf(stderr, "%s : parsing \n", command.fileName);

            SaveParsedFile(fileNameStr, actualModTime);

            clang_disposeTranslationUnit(tu);
            clang_disposeIndex(idx);
        }

        command.Clear();

        pthread_mutex_lock(&g_worklock);
        g_outstandingTasks--;
        pthread_cond_broadcast(&g_finished_cond);
        pthread_mutex_unlock(&g_worklock);
    }
}

PyObject* start_workers(PyObject *self, PyObject *args)
{
    int n_workers;
    if (!PyArg_ParseTuple(args, "i", &n_workers))
    {
        return NULL;
    }

    std::vector<std::thread> workers;

    for (int i = 0; i < n_workers; i++)
    {
        workers.emplace_back(worker);
    }

    for (auto &thread : workers)
    {
        thread.join();
    }
    Py_RETURN_NONE;
}

PyObject* add_file_to_parse(PyObject *self, PyObject *args)
{
    const char* fileName = nullptr;
    PyObject* argList;
    time_t modTime = 0;

    if (!PyArg_ParseTuple(args, "sO!l", &fileName, &PyList_Type, &argList, &modTime))
    {
        return NULL;
    }

    std::vector<std::string> argv;
    for (int i = 0; i < PyList_Size(argList); i++)
    {
        argv.push_back(PyString_AsString(PyList_GetItem(argList, i)));
    }

    CompileCommand cmd(fileName, argv, modTime);

    Py_BEGIN_ALLOW_THREADS;
    pthread_mutex_lock(&g_worklock);
    work.push(cmd);
    g_outstandingTasks++;
    pthread_cond_signal(&g_workcond);
    pthread_mutex_unlock(&g_worklock);
    Py_END_ALLOW_THREADS;

    Py_RETURN_NONE;
}


PyObject* start(PyObject *self, PyObject *args)
{
    PyLevelDB* pyLevelDbConn = nullptr;
    int nworkers = 0;
    
    if (!PyArg_ParseTuple(args, "Oi", &pyLevelDbConn, &nworkers))
        return NULL;

    assert(pyLevelDbConn != nullptr);
    Py_INCREF(pyLevelDbConn);

    for (int i = 0; i < nworkers; i++)
    {
        g_workers.emplace_back(worker);
    }
    for (std::thread& worker : g_workers)
    {
        worker.detach();
    }

    db = pyLevelDbConn->_db;
    Py_RETURN_NONE;
}

PyObject* wait_on_work(PyObject* self, PyObject* args)
{
    Py_BEGIN_ALLOW_THREADS;
    pthread_mutex_lock(&g_worklock);
    while (g_outstandingTasks > 0)
    {
        pthread_cond_wait(&g_finished_cond, &g_worklock);
    }
    pthread_mutex_unlock(&g_worklock);
    Py_END_ALLOW_THREADS;
    Py_RETURN_NONE;
}

PyObject* work_queue_size(PyObject* self, PyObject* args)
{
    int ret = 0;

    Py_BEGIN_ALLOW_THREADS;
    pthread_mutex_lock(&g_worklock);
    ret = g_outstandingTasks;
    pthread_mutex_unlock(&g_worklock);
    Py_END_ALLOW_THREADS;

    return Py_BuildValue("i", ret);
}

PyObject* extract_part(PyObject* self, PyObject* args)
{
    const char* s = nullptr;
    int ordinal = 0;

    if (!PyArg_ParseTuple(args, "si", &s, &ordinal))
    {
        return NULL;
    }

    std::string str(s);

    std::string ret = ExtractPart(str, ordinal);
    return Py_BuildValue("s", ret.c_str());
}

PyObject* remove_file_symbols(PyObject* self, PyObject* args)
{
    const char* s = nullptr;

    if (!PyArg_ParseTuple(args, "s", &s))
    {
        return NULL;
    }

    RemoveFileSymbols(std::string(s));
    Py_RETURN_NONE;
}

static PyMethodDef IndexerMethods[] = {
    {"start", start, METH_VARARGS, "Fill in."},
    {"add_file_to_parse", add_file_to_parse, METH_VARARGS, "Fill in."},
    {"wait_on_work", wait_on_work, METH_VARARGS, "Fill in."},
    {"extract_part", extract_part, METH_VARARGS, "Fill in."},
    {"remove_file_symbols", remove_file_symbols, METH_VARARGS, "Fill in."},
    {"work_queue_size", work_queue_size, METH_VARARGS, "Fill in."},
    {NULL, NULL, 0, NULL}        /* Sentinel */
};

PyMODINIT_FUNC
initindexer(void)
{
    (void) Py_InitModule("indexer", IndexerMethods);
}

int main(void)
{ }
