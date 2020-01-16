# -*- coding: utf-8 -*-

import os
import os.path
import sys
import time
import glob
import http.cookiejar
import tempfile
import lz4.block
import datetime
import configparser

try:
    import json
except ImportError:
    import simplejson as json
try:

    from pysqlite2 import dbapi2 as sqlite3
except ImportError:
    import sqlite3

# external dependencies
import keyring
import pyaes
from pbkdf2 import PBKDF2

__doc__ = 'Loads browser cookies into a cookiejar'


class BrowserCookieError(Exception):
    pass


def create_local_copy(cookie_file):

    if os.path.exists(cookie_file):

        tmp_cookie_file = tempfile.NamedTemporaryFile(suffix='.sqlite').name
        open(tmp_cookie_file, 'wb').write(open(cookie_file, 'rb').read())
        return tmp_cookie_file
    else:
        raise BrowserCookieError('Can not find cookie file at: ' + cookie_file)


def windows_group_policy_path():

    from winreg import ConnectRegistry, HKEY_LOCAL_MACHINE, OpenKeyEx, QueryValueEx, REG_EXPAND_SZ, REG_SZ
    try:
        root = ConnectRegistry(None, HKEY_LOCAL_MACHINE)
        policy_key = OpenKeyEx(root, r"SOFTWARE\Policies\Google\Chrome")
        user_data_dir, type_ = QueryValueEx(policy_key, "UserDataDir")
        if type_ == REG_EXPAND_SZ:
            user_data_dir = os.path.expandvars(user_data_dir)
        elif type_ != REG_SZ:
            return None
    except OSError:
        return None
    return os.path.join(user_data_dir, "Default", "Cookies")


# Code adapted slightly from https://github.com/Arnie97/chrome-cookies
def crypt_unprotect_data(
        cipher_text=b'', entropy=b'', reserved=None, prompt_struct=None
):

    import ctypes
    import ctypes.wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ('cbData', ctypes.wintypes.DWORD),
            ('pbData', ctypes.POINTER(ctypes.c_char))
        ]

    blob_in, blob_entropy, blob_out = map(
        lambda x: DataBlob(len(x), ctypes.create_string_buffer(x)),
        [cipher_text, entropy, b'']
    )
    desc = ctypes.c_wchar_p()

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), ctypes.byref(
                desc), ctypes.byref(blob_entropy),
            reserved, prompt_struct, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(
                blob_out)
    ):
        raise RuntimeError('Failed to decrypt the cipher text with DPAPI')

    description = desc.value
    buffer_out = ctypes.create_string_buffer(int(blob_out.cbData))
    ctypes.memmove(buffer_out, blob_out.pbData, blob_out.cbData)
    map(ctypes.windll.kernel32.LocalFree, [desc, blob_out.pbData])
    return description, buffer_out.value


class Chrome:
    def __init__(self, cookie_file=None, domain_name=""):
        self.salt = b'saltysalt'
        self.iv = b' ' * 16
        self.length = 16

        self.domain_name = domain_name
        if sys.platform == 'darwin':

            my_pass = keyring.get_password('Chrome Safe Storage', 'Chrome').encode(
                'utf8')  # get key from keyring
            iterations = 1003
            self.key = PBKDF2(my_pass, self.salt,
                              iterations=iterations).read(self.length)
            cookie_file = cookie_file \
                or os.path.expanduser('~/Library/Application Support/Google/Chrome/Default/Cookies')

        elif sys.platform.startswith('linux'):

            my_pass = 'peanuts'.encode('utf8')
            iterations = 1
            self.key = PBKDF2(my_pass, self.salt,
                              iterations=iterations).read(self.length)
            cookie_file = cookie_file \
                or os.path.expanduser('~/.config/google-chrome/Default/Cookies') \
                or os.path.expanduser('~/.config/chromium/Default/Cookies') \
                or os.path.expanduser('~/.config/google-chrome-beta/Default/Cookies')
        elif sys.platform == "win32":

            cookie_file = cookie_file or windows_group_policy_path() \
                or glob.glob(os.path.join(os.getenv('APPDATA', ''), '..\Local\\Google\\Chrome\\User Data\\Default\\Cookies')) \
                or glob.glob(os.path.join(os.getenv('LOCALAPPDATA', ''), 'Google\\Chrome\\User Data\\Default\\Cookies')) \
                or glob.glob(os.path.join(os.getenv('APPDATA', ''), 'Google\\Chrome\\User Data\\Default\\Cookies'))
        else:
            raise BrowserCookieError(
                "OS not recognized. Works on Chrome for OSX, Windows, and Linux.")

        if isinstance(cookie_file, list):
            if not cookie_file:
                raise BrowserCookieError('Failed to find Chrome cookie')
            cookie_file = cookie_file[0]

        self.tmp_cookie_file = create_local_copy(cookie_file)

    def __del__(self):

        if hasattr(self, 'tmp_cookie_file'):
            os.remove(self.tmp_cookie_file)

    def __str__(self):
        return 'chrome'

    def load(self):
        """Load sqlite cookies into a cookiejar
        """
        con = sqlite3.connect(self.tmp_cookie_file)
        cur = con.cursor()
        try:
            cur.execute('SELECT host_key, path, secure, expires_utc, name, value, encrypted_value '
                        'FROM cookies WHERE host_key like "%{}%";'.format(self.domain_name))
        except sqlite3.OperationalError:
            cur.execute('SELECT host_key, path, is_secure, expires_utc, name, value, encrypted_value '
                        'FROM cookies WHERE host_key like "%{}%";'.format(self.domain_name))

        cj = http.cookiejar.CookieJar()
        epoch_start = datetime.datetime(1601, 1, 1)
        for item in cur.fetchall():
            host, path, secure, expires, name = item[:5]
            if item[3] != 0:
                offset = min(int(item[3]), 265000000000000000)
                delta = datetime.timedelta(microseconds=offset)
                expires = epoch_start + delta
                expires = expires.timestamp()
            value = self._decrypt(item[5], item[6])
            c = create_cookie(host, path, secure, expires, name, value)
            cj.set_cookie(c)
        con.close()
        return cj

    @staticmethod
    def _decrypt_windows_chrome(value, encrypted_value):

        if len(value) != 0:
            return value

        if encrypted_value == "":
            return ""

        _, data = crypt_unprotect_data(encrypted_value)
        assert isinstance(data, bytes)
        return data.decode()

    def _decrypt(self, value, encrypted_value):
        """Decrypt encoded cookies
        """

        if sys.platform == 'win32':
            return self._decrypt_windows_chrome(value, encrypted_value)

        if value or (encrypted_value[:3] != b'v10'):
            return value

        encrypted_value = encrypted_value[3:]
        encrypted_value_half_len = int(len(encrypted_value) / 2)

        cipher = pyaes.Decrypter(
            pyaes.AESModeOfOperationCBC(self.key, self.iv))
        decrypted = cipher.feed(encrypted_value[:encrypted_value_half_len])
        decrypted += cipher.feed(encrypted_value[encrypted_value_half_len:])
        decrypted += cipher.feed()
        return decrypted.decode("utf-8")


class Firefox:
    def __init__(self, cookie_file=None, domain_name=""):
        self.tmp_cookie_file = None
        cookie_file = cookie_file or self.find_cookie_file()
        self.tmp_cookie_file = create_local_copy(cookie_file)

        self.session_file = os.path.join(
            os.path.dirname(cookie_file), 'sessionstore.js')
        self.session_file_lz4 = os.path.join(os.path.dirname(
            cookie_file), 'sessionstore-backups', 'recovery.jsonlz4')

        self.domain_name = domain_name

    def __del__(self):

        if self.tmp_cookie_file:
            os.remove(self.tmp_cookie_file)

    def __str__(self):
        return 'firefox'

    def get_default_profile(self, profiles_ini_path, template_for_relative):
        """ Given the path to firefox profiles.ini,
            will return relative path to firefox default profile
        """
        config = configparser.ConfigParser()
        config.read(profiles_ini_path)
        for section in config.sections():
            try:
                if config[section]['Default'] == '1' and config[section]['IsRelative'] == '1':
                    return template_for_relative.format(config[section]['Path'])
            except KeyError:
                continue
        return None

    def find_cookie_file(self):
        if sys.platform == 'darwin':
            profiles_ini_paths = glob.glob(os.path.expanduser(
                '~/Library/Application Support/Firefox/profiles.ini'))
            profiles_ini_path = self.get_default_profile(profiles_ini_paths, os.path.expanduser(
                '~/Library/Application Support/Firefox/Profiles/{0}/cookies.sqlite'.format(profiles_ini_path)))
            cookie_files = glob.glob(
                os.path.expanduser('~/Library/Application Support/Firefox/Profiles/*default/cookies.sqlite')) \
                or glob.glob(profiles_ini_path)
        elif sys.platform.startswith('linux'):
            profiles_ini_paths = glob.glob(
                os.path.expanduser('~/.mozilla/firefox/profiles.ini'))
            profiles_ini_path = self.get_default_profile(
                profiles_ini_paths, os.path.expanduser('~/.mozilla/firefox/{0}/cookies.sqlite'))
            cookie_files = glob.glob(os.path.expanduser('~/.mozilla/firefox/*default*/cookies.sqlite')) \
                or glob.glob(profiles_ini_path)
        elif sys.platform == 'win32':
            profiles_ini_paths = glob.glob(os.path.join(os.environ.get('APPDATA', ''),
                                                        'Mozilla/Firefox/profiles.ini')) \
                or glob.glob(os.path.join(os.environ.get('LOCALAPPDATA', ''),
                                          'Mozilla/Firefox/profiles.ini'))
            profiles_ini_path = self.get_default_profile(profiles_ini_paths, os.path.join(os.environ.get('APPDATA', ''),
                                                                                          "Mozilla/Firefox/{0}/cookies.sqlite"))
            cookie_files = glob.glob(os.path.join(os.environ.get('PROGRAMFILES', ''),
                                                  'Mozilla Firefox/profile/cookies.sqlite')) \
                or glob.glob(os.path.join(os.environ.get('PROGRAMFILES(X86)', ''),
                                          'Mozilla Firefox/profile/cookies.sqlite')) \
                or glob.glob(os.path.join(os.environ.get('APPDATA', ''),
                                          'Mozilla/Firefox/Profiles/*default*/cookies.sqlite')) \
                or glob.glob(os.path.join(os.environ.get('LOCALAPPDATA', ''),
                                          'Mozilla/Firefox/Profiles/*default*/cookies.sqlite')) \
                or glob.glob(profiles_ini_path)
        else:
            raise BrowserCookieError(
                'Unsupported operating system: ' + sys.platform)
        if cookie_files:
            return cookie_files[0]
        else:
            raise BrowserCookieError('Failed to find Firefox cookie')

    @staticmethod
    def __create_session_cookie(cookie_json):
        expires = str(int(time.time()) + 3600 * 24 * 7)
        return create_cookie(cookie_json.get('host', ''), cookie_json.get('path', ''), False, expires,
                             cookie_json.get('name', ''), cookie_json.get('value', ''))

    def __add_session_cookies(self, cj):
        if not os.path.exists(self.session_file):
            return
        try:
            json_data = json.loads(
                open(self.session_file, 'rb').read().decode())
        except ValueError as e:
            print('Error parsing firefox session JSON:', str(e))
        else:
            for window in json_data.get('windows', []):
                for cookie in window.get('cookies', []):
                    cj.set_cookie(Firefox.__create_session_cookie(cookie))

    def __add_session_cookies_lz4(self, cj):
        if not os.path.exists(self.session_file_lz4):
            return
        try:
            file_obj = open(self.session_file_lz4, 'rb')
            file_obj.read(8)
            json_data = json.loads(lz4.block.decompress(file_obj.read()))
        except ValueError as e:
            print('Error parsing firefox session JSON LZ4:', str(e))
        else:
            for cookie in json_data.get('cookies', []):
                cj.set_cookie(Firefox.__create_session_cookie(cookie))

    def load(self):
        con = sqlite3.connect(self.tmp_cookie_file)
        cur = con.cursor()
        cur.execute('select host, path, isSecure, expiry, name, value from moz_cookies '
                    'where host like "%{}%"'.format(self.domain_name))

        cj = http.cookiejar.CookieJar()
        for item in cur.fetchall():
            c = create_cookie(*item)
            cj.set_cookie(c)
        con.close()

        self.__add_session_cookies(cj)
        self.__add_session_cookies_lz4(cj)

        return cj


def load(domain_name=""):
    """Try to load cookies from all supported browsers and return combined cookiejar
    Optionally pass in a domain name to only load cookies from the specified domain
    """
    cj = http.cookiejar.CookieJar()
    for cookie_fn in [chrome, firefox]:
        try:
            for cookie in cookie_fn(domain_name=domain_name):
                cj.set_cookie(cookie)
        except BrowserCookieError:
            pass
    return cj


if __name__ == '__main__':
    pass
