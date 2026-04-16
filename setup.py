from setuptools import setup, find_packages

setup(
    name="webaa-sdk",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "httpx>=0.27,<1.0",
    ],
)
