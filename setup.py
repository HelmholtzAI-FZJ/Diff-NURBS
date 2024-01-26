from setuptools import find_packages, setup

setup(
    name='diff_nurbs',
    python_requires='>=3.6,<3.12',
    version='0.0.1',
    packages=find_packages(),
    install_requires=[
        'torch>=1.8,<3.0',
        'matplotlib>=3.4,<4.0',
    ],
    author='Jan Ebert',
    author_email='ja.ebert@fz-juelich.de',
    description=(
        'An automatically differentiable pure-Python NURBS '
        'implementation in PyTorch.'
    ),
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: OS Independent',
    ],
    url='https://github.com/HelmholtzAI-FZJ/Diff-NURBS',
)
