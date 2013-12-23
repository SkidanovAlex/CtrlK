from ctrlk import project

if __name__ == '__main__':
    project = project.Project('/usr/local/lib', '/home/ankur/memsql')
    project.scan_and_index()
    project.wait_on_work()
