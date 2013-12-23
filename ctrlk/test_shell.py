from IPython import embed

from ctrlk import project
from ctrlk import indexer
from ctrlk import search

if __name__ == '__main__':
    project = project.Project('/usr/local/lib', '/home/ankur/memsql')
    embed()
