"""
Flask-S3-gzip
-------------

Easily serve your static files from Amazon S3.
"""
from setuptools import setup


setup(
    name='Flask-S3-gzip',
    version='0.1.6b3',
    url='http://github.com/spoqa/flask-s3',
    license='MIT License',
    author='xymz',
    maintainer='Spoqa',
    author_email='xym@spoqa.com',
    maintainer_email='dev@spoqa.com',
    description='Seamlessly serve the static files of your Flask app from Amazon S3 (forked from original flask-s3)',
    long_description=__doc__,
    py_modules=['flask_s3'],
    zip_safe=False,
    include_package_data=True,
    platforms='any',
    install_requires=[
        'Flask',
        'Boto>=2.5.2',
        'futures'
    ],
    tests_require=['nose', 'mock'],
    classifiers=[
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: Other/Proprietary License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ]
)
