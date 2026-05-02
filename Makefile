.PHONY: all rust python one clean-cache build

all:
	python3 gen.py

rust:
	python3 gen.py --engine rust

python:
	python3 gen.py --engine python

one:
	python3 gen.py --only jsl

build:
	cargo build --release --manifest-path rust/theorybib-dblp/Cargo.toml

clean-cache:
	rm -rf .cache/dblp
