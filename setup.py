from setuptools import setup, find_packages

setup(
    name="agenthub-sdk",
    version="1.0.0",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "httpx>=0.27,<1.0",
    ],
)
