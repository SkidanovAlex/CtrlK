all: build

clean:
	python setup.py clean && rm -rf build/*

veryclean:
	python setup.py clean && rm -rf build/*
	rm -rf py-leveldb

build: .FORCE py-leveldb
	python setup.py build
	sudo pip install -e .

test: build
	python test_cpp_indexer.py

clean_test:
	rm -rf ~/memsql/.ctrlk-index && python test_cpp_indexer.py

py-leveldb:
	./get_pyleveldb.sh

install: build py-leveldb
	cd py-leveldb && sudo pip install -e .
	sudo pip install -e .

shell:
	python test_shell.py

.FORCE:


