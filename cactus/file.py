import os
import logging
import hashlib
import socket
from cactus.utils.file import compressString, fileSize
from cactus.utils.helpers import CaseInsensitiveDict, memoize
from cactus.utils.network import retry
from cactus.utils.url import getURLHeaders
import mime
import copy


class File(object):
    CACHE_EXPIRATION = 60 * 60 * 24 * 7  # One week
    COMPRESS_TYPES = ['html', 'css', 'js', 'txt', 'xml']
    COMPRESS_MIN_SIZE = 1024  # 1kb
    PROGRESS_MIN_SIZE = (1024 * 1024) / 2  # 521 kb

    def __init__(self, site, path):
        self.site = site
        self.path = path

    @memoize
    def data(self):
        with open(os.path.join(self.site.path, '.build', self.path)) as f:
            return f.read()

    def payload(self):
        """
        The representation of the data that should be uploaded to the
        server. This might be compressed based on the content type and size.
        """
        if not hasattr(self, '_payload'):
            if self.shouldCompress():
                self._payload = compressString(self.data())
            else:
                self._payload = self.data()

        return self._payload

    def checksum(self):
        """
        An amazon compatible md5 of the payload data.
        """
        return hashlib.md5(self.payload()).hexdigest()

    def remoteChecksum(self):
        return getURLHeaders(self.remoteURL()).get('etag', '').strip('"')

    def remoteURL(self):
        return 'http://%s/%s' % (self.site.config.get('aws-bucket-website'), self.path)

    def extension(self):
        return os.path.splitext(self.path)[1].strip('.').lower()

    def shouldCompress(self):

        if not self.extension() in self.COMPRESS_TYPES:
            return False

        if len(self.data()) < self.COMPRESS_MIN_SIZE:
            return False

        return True


    def changed(self):
        remote_headers = {k: v.strip('"') for k, v in getURLHeaders(self.remoteURL()).items()}
        local_headers = copy.copy(self.headers)  # Will do a copy.
        local_headers['etag'] = self.checksum()
        for k,v in local_headers.items():  # Don't check AWS' own headers.
            if remote_headers.get(k) != v:
                return True
        return False

    @retry(socket.error, tries = 5, delay = 3, backoff = 2)
    def upload(self, bucket):
        self.lastUpload = 0

        self.headers = CaseInsensitiveDict((('Cache-Control', 'max-age=%s' % self.CACHE_EXPIRATION),))

        if self.shouldCompress():
            self.headers['Content-Encoding'] = 'gzip'

        self.site.plugin_manager.preDeployFile(self)

        changed = self.changed()

        if changed:

            # Show progress if the file size is big
            progressCallback = None
            progressCallbackCount = int(len(self.payload()) / (1024 * 1024))

            if len(self.payload()) > self.PROGRESS_MIN_SIZE:
                def progressCallback(current, total):
                    if current > self.lastUpload:
                        uploadPercentage = (float(current) / float(total)) * 100
                        logging.info('+ %s upload progress %.1f%%' % (self.path, uploadPercentage))
                        self.lastUpload = current

            # Create a new key from the file path and guess the mime type
            key = bucket.new_key(self.path)
            mimeType = mime.guess(self.path)

            if mimeType:
                key.content_type = mimeType

            # Upload the data
            key.set_contents_from_string(self.payload(), self.headers,
                                         policy = 'public-read',
                                         cb = progressCallback,
                                         num_cb = progressCallbackCount)

        op1 = '+' if changed else '-'
        op2 = ' (%s compressed)' % (fileSize(len(self.payload()))) if self.shouldCompress() else ''

        logging.info('%s %s - %s%s' % (op1, self.path, fileSize(len(self.data())), op2))

        return {'changed': changed, 'size': len(self.payload())}

    def __repr__(self):
        return '<File: {0}>'.format(self.path)
