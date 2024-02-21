#!/usr/bin/env python

from setuptools import setup
import re


def find_version():
    return re.search(r"^__version__ = '(.*)'$",
                     open('moblin_assistant.py', 'r').read(),
                     re.MULTILINE).group(1)

setup(name='moblin_assistant',
      version=find_version(),
      description='Moblin remote control assistant',
      long_description=open('README.rst', 'r').read(),
      author='Erik Moqvist',
      author_email='erik.moqvist@gmail.com',
      license='MIT',
      classifiers=[
          'License :: OSI Approved :: MIT License',
          'Programming Language :: Python :: 3.9',
      ],
      url='https://github.com/eerimoq/moblin_assistant',
      py_modules=['moblin_assistant'],
      install_requires=[
          'aiohttp',
          'websockets'
      ],
      python_requires='>=3.9',
      entry_points = {
          'console_scripts': ['moblin_assistant=moblin_assistant:main']
      })
