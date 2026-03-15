from setuptools import setup, find_packages

setup(
    name="openclaw-memory",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "pyyaml>=6.0",
        "requests>=2.31",
        "redis>=5.0",
        "qdrant-client>=1.9",
    ],
    entry_points={
        "console_scripts": [
            "oc-memory=openclaw_memory.cli:main",
        ],
    },
    python_requires=">=3.10",
)
