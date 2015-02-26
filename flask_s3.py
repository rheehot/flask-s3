from concurrent.futures import ThreadPoolExecutor
import os
import re
import gzip
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO
import logging
import mimetypes
from collections import defaultdict

from flask import url_for as flask_url_for
from flask import current_app, request
from boto.s3.connection import S3Connection
from boto.exception import S3CreateError, S3ResponseError
from boto.s3.key import Key




logger = logging.getLogger('flask_s3')
mimetypes.add_type('text/css', '.less')


def url_for(endpoint, **values):
    """
    Generates a URL to the given endpoint.

    If the endpoint is for a static resource then an Amazon S3 URL is 
    generated, otherwise the call is passed on to `flask.url_for`.

    Because this function is set as a jinja environment variable when 
    `FlaskS3.init_app` is invoked, this function replaces 
    `flask.url_for` in templates automatically. It is unlikely that this
    function will need to be directly called from within your 
    application code, unless you need to refer to static assets outside 
    of your templates.
    """
    app = current_app
    if 'S3_BUCKET_NAME' not in app.config:
        raise ValueError("S3_BUCKET_NAME not found in app configuration.")

    if app.debug and not app.config['USE_S3_DEBUG']:
        return flask_url_for(endpoint, **values)

    if endpoint == 'static' or endpoint.endswith('.static'):
        scheme = 'http'
        scheme = app.config['S3_URL_SCHEME'] or 'https'
        bucket_path = '%s.%s' % (app.config['S3_BUCKET_NAME'],
                                 app.config['S3_BUCKET_DOMAIN'])
        if app.config['S3_CDN_DOMAIN']:
            bucket_path = '%s' % app.config['S3_CDN_DOMAIN']
        if app.config['S3_PREFIX']:
            bucket_path = "/".join((bucket_path, app.config['S3_PREFIX']))
        urls = app.url_map.bind(bucket_path, url_scheme=scheme)
        try:
            mimetype = mimetypes.guess_type(values['filename'])[0]
        except KeyError:
            mimetype = None
        if app.config['USE_GZIP']:
            accept_encoding = request.headers.get('Accept-Encoding', '')
            if (mimetype in app.config['S3_GZIP_CONTENT_TYPES'] and
                'gzip' in accept_encoding.lower()):
                    values['filename'] += '.gz'
        url = urls.build(endpoint, values=values, force_external=True)
        if app.config['S3_URL_SCHEME'] is None:
            url = re.sub(r'^https://', '//', url)
        return url
    return flask_url_for(endpoint, **values)


def _bp_static_url(blueprint):
    """ builds the absolute url path for a blueprint's static folder """
    u = u'%s%s' % (blueprint.url_prefix or '', blueprint.static_url_path or '')
    return u


def _gather_files(app, hidden):
    """ Gets all files in static folders and returns in dict."""
    dirs = [(unicode(app.static_folder), app.static_url_path)]
    if hasattr(app, 'blueprints'):
        blueprints = app.blueprints.values()
        bp_details = lambda x: (x.static_folder, _bp_static_url(x))
        dirs.extend([bp_details(x) for x in blueprints if x.static_folder])

    valid_files = defaultdict(list)
    for static_folder, static_url_loc in dirs:
        if not os.path.isdir(static_folder):
            logger.warning("WARNING - [%s does not exist]" % static_folder)
        else:
            logger.debug("Checking static folder: %s" % static_folder)
        for root, _, files in os.walk(static_folder):
            files = [os.path.join(root, x) \
                     for x in files if hidden or x[0] != '.']
            if files:
                valid_files[(static_folder, static_url_loc)].extend(files)
    return valid_files


def _path_to_relative_url(path):
    """ Converts a folder and filename into a ralative url path """
    return os.path.splitdrive(path)[1].replace('\\', '/')


def _static_folder_path(static_url, static_folder, static_asset, prefix=''):
    """ 
    Returns a path to a file based on the static folder, and not on the 
    filesystem holding the file.

    Returns a path relative to static_url for static_asset
    """
    # first get the asset path relative to the static folder. 
    # static_asset is not simply a filename because it could be 
    # sub-directory then file etc.
    if not static_asset.startswith(static_folder):
        raise ValueError("%s startic asset must be under %s static folder" %
                         (static_asset, static_folder))
    rel_asset = static_asset[len(static_folder):]
    # Now bolt the static url path and the relative asset location together
    key = u'%s/%s' % (static_url.rstrip('/'), rel_asset.lstrip('/'))
    if prefix:
        key = u'%s/%s' % (prefix, key)
    return key


def _write_files(app, static_url_loc, static_folder, files, bucket,
                 ex_keys=None):
    """ Writes all the files inside a static folder to S3. """
    with ThreadPoolExecutor(app.config['S3_UPLOAD_COCURRENCY']) as executor:
        for file_path in files:
            asset_loc = _path_to_relative_url(file_path)
            key_name = _static_folder_path(static_url_loc, static_folder,
                                           asset_loc, app.config['S3_PREFIX'])
            mimetype = mimetypes.guess_type(key_name)[0]
            is_gzippable = mimetype in app.config['S3_GZIP_CONTENT_TYPES']
            headers = app.config['S3_HEADERS']
            msg = "Uploading %s to %s as %s" % (file_path, bucket, key_name)
            logger.debug(msg)
            if ex_keys and key_name in ex_keys:
                logger.debug("%s excluded from upload" % key_name)
            else:
                do_gzip = app.config['USE_GZIP'] and is_gzippable
                # upload origin file
                executor.submit(_upload_file, file_path, bucket, key_name, headers)
                # upload gzipped file (if enabled)
                if do_gzip:
                    gzip_key_name = "%s.gz" % key_name
                    executor.submit(_upload_file, file_path, bucket, gzip_key_name, headers, True)

def _upload_file(file_path, bucket, key_name, headers={}, do_gzip=False):
    k = Key(bucket=bucket, name=key_name)
    for header, value in headers.items():
        if (header, value) != ('Content-Encoding', 'gzip'):
            k.set_metadata(header, value)
    mimetype = mimetypes.guess_type(file_path)[0]
    if mimetype:
        k.set_metadata('Content-Type', mimetype)
    with open(file_path) as f:
        content = f.read()
        if do_gzip:
            k.set_metadata('Content-Encoding', 'gzip')
            gzipped = StringIO()
            with gzip.GzipFile(fileobj=gzipped, mode='w') as _gzip:
                _gzip.write(content)
            content = gzipped.getvalue()
    try:
        k.set_contents_from_string(content)
    except S3ResponseError:
        if not do_gzip:
            k.set_contents_from_filename(file_path)
        else:
            raise
    k.make_public()
    return k

def _upload_files(app, files_, bucket):
    for (static_folder, static_url), names in files_.iteritems():
        _write_files(app, static_url, static_folder, names, bucket)


def create_all(app, user=None, password=None, bucket_name=None,
               location='', include_hidden=False, force_refresh=False):
    """
    Uploads of the static assets associated with a Flask application to 
    Amazon S3.

    All static assets are identified on the local filesystem, including 
    any static assets associated with *registered* blueprints. In turn, 
    each asset is uploaded to the bucket described by `bucket_name`. If 
    the bucket does not exist then it is created.

    Flask-S3 creates the same relative static asset folder structure on
    S3 as can be found within your Flask application.

    Many of the optional arguments to `create_all` can be specified
    instead in your application's configuration using the Flask-S3
    `configuration`_ variables.

    :param app: a :class:`flask.Flask` application object.

    :param user: an AWS Access Key ID. You can find this key in the
                 Security Credentials section of your AWS account.
    :type user: `basestring` or None

    :param password: an AWS Secret Access Key. You can find this key in
                     the Security Credentials section of your AWS 
                     account.
    :type password: `basestring` or None
    
    :param bucket_name: the name of the bucket you wish to server your
                        static assets from. **Note**: while a valid 
                        character, it is recommended that you do not 
                        include periods in bucket_name if you wish to 
                        serve over HTTPS. See Amazon's `bucket
                        restrictions`_ for more details.
    :type bucket_name: `basestring` or None

    :param location: the AWS region to host the bucket in; an empty
                     string indicates the default region should be used,
                     which is the US Standard region. Possible location 
                     values include: `'DEFAULT'`, `'EU'`, `'USWest'`, 
                     `'APSoutheast'`
    :type location: `basestring` or None

    :param include_hidden: by default Flask-S3 will not upload hidden
        files. Set this to true to force the upload of hidden files.
    :type include_hidden: `bool`

    .. _bucket restrictions: http://docs.amazonwebservices.com/AmazonS3\
    /latest/dev/BucketRestrictions.html

    """
    if user is None and 'AWS_ACCESS_KEY_ID' in app.config:
        user = app.config['AWS_ACCESS_KEY_ID']
    if password is None and 'AWS_SECRET_ACCESS_KEY' in app.config:
        password = app.config['AWS_SECRET_ACCESS_KEY']
    if bucket_name is None and 'S3_BUCKET_NAME' in app.config:
        bucket_name = app.config['S3_BUCKET_NAME']
    if not bucket_name:
        raise ValueError("No bucket name provided.")
    # build list of static files
    all_files = _gather_files(app, include_hidden)
    logger.debug("All valid files: %s" % all_files)
    conn = S3Connection(user, password) # connect to s3
    # get_or_create bucket
    try:
        try:
            bucket = conn.create_bucket(bucket_name, location=location)
            bucket.make_public(recursive=True)
        except S3CreateError as e:
            if e.error_code == u'BucketAlreadyOwnedByYou':
                bucket = conn.get_bucket(bucket_name)
                if force_refresh:
                    bucket.make_public(recursive=True)
            else:
                raise e
    except S3CreateError as e:
        raise e
    _upload_files(app, all_files, bucket)


class FlaskS3(object):
    """
    The FlaskS3 object allows your application to use Flask-S3.

    When initialising a FlaskS3 object you may optionally provide your 
    :class:`flask.Flask` application object if it is ready. Otherwise, 
    you may provide it later by using the :meth:`init_app` method.

    :param app: optional :class:`flask.Flask` application object
    :type app: :class:`flask.Flask` or None
    """
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """
        An alternative way to pass your :class:`flask.Flask` application
        object to Flask-S3. :meth:`init_app` also takes care of some 
        default `settings`_.

        :param app: the :class:`flask.Flask` application object.
        """
        defaults = [('S3_URL_SCHEME', None),
                    ('S3_USE_HTTPS', None),
                    ('USE_S3', True),
                    ('USE_GZIP', False),
                    ('USE_S3_DEBUG', False),
                    ('S3_BUCKET_DOMAIN', 's3.amazonaws.com'),
                    ('S3_CDN_DOMAIN', ''),
                    ('S3_USE_CACHE_CONTROL', False),
                    ('S3_HEADERS', {}),
                    ('S3_GZIP_CONTENT_TYPES', (
                        'text/css',
                        'application/javascript',
                        'application/x-javascript',
                    )),
                    ('S3_PREFIX', None),
                    ('S3_UPLOAD_COCURRENCY', 32)]
        for k, v in defaults:
            app.config.setdefault(k, v)

        if app.config['USE_S3']:
            app.jinja_env.globals['url_for'] = url_for
        if app.config['S3_USE_CACHE_CONTROL'] and 'S3_CACHE_CONTROL' in app.config:
            cache_control_header = app.config['S3_CACHE_CONTROL']
            app.config['S3_HEADERS']['Cache-Control'] = cache_control_header
        if app.config['S3_URL_SCHEME'] is None and \
           app.config['S3_USE_HTTPS'] is not None:
            scheme = 'https' if app.config['S3_USE_HTTPS'] else 'http'
            app.config['S3_URL_SCHEME'] = scheme

