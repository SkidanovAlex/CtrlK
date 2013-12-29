import argparse
import json
import os
import signal
import threading
import time
import tornado.web

from ctrlk import client_api
from ctrlk import project
from ctrlk import search

g_project = None
g_last_request_time = time.time()

def get_absolute_path():
    return os.path.abspath(os.path.realpath(__file__))

def killer_thread(suicide_seconds):
    global g_last_request_time
    while True:
        if time.time() - g_last_request_time > suicide_seconds:
            os.kill(os.getpid(), signal.SIGINT)
        time.sleep(10)

class MyRequestHandler(tornado.web.RequestHandler):
    def prepare(self):
        global g_last_request_time
        g_last_request_time = time.time()

class PingHandler(MyRequestHandler):
    def get(self):
        self.write("Hello, world!")

class RegisterHandler(MyRequestHandler):
    def get(self):
        global g_project

        library_path = self.get_argument("library_path")
        project_root = self.get_argument("project_root")

        abs_project_root = os.path.abspath(project_root)

        if g_project and g_project.project_root not in abs_project_root:
            self.set_status(400)
            self.write("Already running with a different project: %s" % (g_project.project_root))
            return

        if g_project is None:
            g_project = project.Project(library_path, project_root)

class ParseHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument("file_name", None)
        if file_name:
            g_project.parse_file(file_name)
        else:
            g_project.scan_and_index()

class QueueSizeHandler(MyRequestHandler):
    def get(self):
        self.write(json.dumps(g_project.work_queue_size()))

class LevelDBSearchHandler(MyRequestHandler):
    def get(self):
        starts_with = self.get_argument('starts_with')
        ret = [x for x in search.leveldb_range_iter(g_project.leveldb_connection, starts_with)]
        self.write(json.dumps(ret))

class MatchHandler(MyRequestHandler):
    def get(self):
        prefix = self.get_argument('prefix')
        limit = int(self.get_argument('limit'))
        ret = search.get_items_matching_pattern(g_project.leveldb_connection, prefix, limit)
        self.write(json.dumps(ret))

class BuiltinHeaderPathHandler(MyRequestHandler):
    def get(self):
        self.write(json.dumps(g_project.builtin_header_path))

class FileArgsHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument('file_name')

        origin_file, compile_command, mod_time = g_project.get_file_args(file_name)
        self.write(json.dumps(compile_command))

def sigint_handler(signum, frame):
    if g_project:
        g_project.wait_on_work()
    os.kill(os.getpid(), signal.SIGTERM)

application = tornado.web.Application([
    (r"/", PingHandler),
    (r"/register", RegisterHandler),
    (r"/parse", ParseHandler),
    (r"/queue_size", QueueSizeHandler),
    (r"/leveldb_search", LevelDBSearchHandler),
    (r"/match", MatchHandler),
    (r"/builtin_header_path", BuiltinHeaderPathHandler),
    (r"/file_args", FileArgsHandler),
])

def launch_server(port, suicide_seconds):
    application.listen(port)

    signal.signal(signal.SIGINT, sigint_handler)

    t = threading.Thread(target=killer_thread, args=(suicide_seconds,))
    t.start()

    tornado.ioloop.IOLoop.instance().start()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-p', '--port', dest='port', type=int, default=client_api.DEFAULT_PORT)
    parser.add_argument('-s', '--suicide-seconds', dest='suicide_seconds', type=int, default=3600)
    options = parser.parse_args()

    launch_server(options.port, options.suicide_seconds)

