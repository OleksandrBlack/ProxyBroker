import asyncio
import re
import warnings
from base64 import b64decode
from datetime import datetime, timedelta
from html import unescape
from math import sqrt
from urllib.parse import unquote, urlparse

import aiohttp

from .errors import BadStatusError
from .utils import IPPattern, IPPortPatternGlobal, get_headers, log


class Provider:
    """Proxy provider.

    Provider - a website that publish free public proxy lists.

    :param str url: Url of page where to find proxies
    :param tuple proto:
        (optional) List of the types (protocols) that may be supported
        by proxies returned by the provider. Then used as :attr:`Proxy.types`
    :param int max_conn:
        (optional) The maximum number of concurrent connections on the provider
    :param int max_tries:
        (optional) The maximum number of attempts to receive response
    :param int timeout:
        (optional) Timeout of a request in seconds
    """

    _pattern = IPPortPatternGlobal

    def __init__(
        self, url=None, proto=(), max_conn=4, max_tries=3, timeout=20, loop=None
    ):
        if url:
            self.domain = urlparse(url).netloc
        self.url = url
        self.proto = proto
        self._max_tries = max_tries
        self._timeout = timeout
        self._session = None
        self._cookies = {}
        self._proxies = set()
        # concurrent connections on the current provider
        self._sem_provider = asyncio.Semaphore(max_conn)
        self._loop = loop or asyncio.get_event_loop()

    @property
    def proxies(self):
        """Return all found proxies.

        :return:
            Set of tuples with proxy hosts, ports and types (protocols)
            that may be supported (from :attr:`.proto`).

            For example:
                {('192.168.0.1', '80', ('HTTP', 'HTTPS'), ...)}

        :rtype: set
        """
        return self._proxies

    @proxies.setter
    def proxies(self, new):
        new = [(host, port, self.proto) for host, port in new if port]
        self._proxies.update(new)

    async def get_proxies(self):
        """Receive proxies from the provider and return them.

        :return: :attr:`.proxies`
        """
        log.debug('Try to get proxies from %s' % self.domain)

        async with aiohttp.ClientSession(
            headers=get_headers(), cookies=self._cookies, loop=self._loop
        ) as self._session:
            await self._pipe()

        log.debug(
            '%d proxies received from %s: %s'
            % (len(self.proxies), self.domain, self.proxies)
        )
        return self.proxies

    async def _pipe(self):
        await self._find_on_page(self.url)

    async def _find_on_pages(self, urls):
        if not urls:
            return
        tasks = []
        if not isinstance(urls[0], dict):
            urls = set(urls)
        for url in urls:
            if isinstance(url, dict):
                tasks.append(self._find_on_page(**url))
            else:
                tasks.append(self._find_on_page(url))
        await asyncio.gather(*tasks)

    async def _find_on_page(self, url, data=None, headers=None, method='GET'):
        page = await self.get(url, data=data, headers=headers, method=method)
        if not page:
            return
        oldcount = len(self.proxies)
        try:
            received = self.find_proxies(page)
        except Exception as e:
            received = []
            log.error(
                'Error when executing find_proxies.'
                'Domain: %s; Error: %r' % (self.domain, e)
            )
        self.proxies = received
        added = len(self.proxies) - oldcount
        log.debug(
            '%d(%d) proxies added(received) from %s'
            % (added, len(received), url)
        )

    async def get(self, url, data=None, headers=None, method='GET'):
        for _ in range(self._max_tries):
            page = await self._get(
                url, data=data, headers=headers, method=method
            )
            if page:
                break
        return page

    async def _get(self, url, data=None, headers=None, method='GET'):
        page = ''
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with self._sem_provider, self._session.request(
                method, url, data=data, headers=headers, timeout=timeout,
                # proxy='http://localhost:8888'  # for Fiddler
            ) as resp:
                page = await resp.text()
                if resp.status != 200:
                    log.debug(
                        'url: %s\nheaders: %s\ncookies: %s\npage:\n%s'
                        % (url, resp.headers, resp.cookies, page)
                    )
                    raise BadStatusError('Status: %s' % resp.status)
        except Exception as e:
            log.debug('%s is failed. Error: %r;' % (url, e))
        return page

    def find_proxies(self, page):
        return self._find_proxies(page)

    def _find_proxies(self, page):
        proxies = self._pattern.findall(page)
        return proxies



class Blogspot_com_base(Provider):
    _cookies = {'NCR': 1}

    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*\.\w+/\d{4}/\d{2}/[^'"#]*)['"]>'''
        pages = await asyncio.gather(
            *[self.get('http://%s/' % d) for d in self.domains]
        )
        urls = re.findall(exp, ''.join(pages))
        await self._find_on_pages(urls)


class Blogspot_com(Blogspot_com_base):
    domain = 'blogspot.com'
    domains = [
        'sslproxies24.blogspot.com',
        'proxyserverlist-24.blogspot.com',
        'freeschoolproxy.blogspot.com',
        'googleproxies24.blogspot.com',
    ]


class Webanetlabs_net(Provider):
    domain = 'webanetlabs.net'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]*proxylist2022[^'"]*)['"]'''
        page = await self.get('https://webanetlabs.net/publ/24')
        if not page:
            return
        urls = [
            'https://webanetlabs.net%s' % path for path in re.findall(exp, page)
        ]
        await self._find_on_pages(urls)


class Checkerproxy_net(Provider):
    domain = 'checkerproxy.net'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"](/archive/\d{4}-\d{2}-\d{2})['"]'''
        page = await self.get('https://checkerproxy.net/')
        if not page:
            return
        urls = [
            'https://checkerproxy.net/api%s' % path
            for path in re.findall(exp, page)
        ]
        await self._find_on_pages(urls)



class Proxy_list_org(Provider):
    domain = 'proxy-list.org'
    _pattern = re.compile(r'''Proxy\('([\w=]+)'\)''')

    def find_proxies(self, page):
        return [
            b64decode(hp).decode().split(':') for hp in self._find_proxies(page)
        ]

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]\./([^'"]?index\.php\?p=\d+[^'"]*)['"]'''
        url = 'http://proxy-list.org/english/index.php?p=1'
        page = await self.get(url)
        if not page:
            return
        urls = [
            'http://proxy-list.org/english/%s' % path
            for path in re.findall(exp, page)
        ]
        urls.append(url)
        await self._find_on_pages(urls)



class Foxtools_ru(Provider):
    domain = 'foxtools.ru'

    async def _pipe(self):
        urls = [
            'http://api.foxtools.ru/v2/Proxy.txt?page=%d' % n
            for n in range(1, 6)
        ]
        await self._find_on_pages(urls)


class proxyservers_pro(Provider):
    domain = 'premproxy.com'

    async def _pipe(self):
        urls = [
            'https://premproxy.com/list/ip-port/%d.htm' % n
            for n in range(1, 20)
        ]
        await self._find_on_pages(urls)

class Xseo_in(Provider):
    domain = 'xseo.in'
    charEqNum = {}

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(""\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        await self._find_on_page(
            url='http://xseo.in/proxylist', data={'submit': 1}, method='POST'
        )


class Nntime_com(Provider):
    domain = 'nntime.com'
    charEqNum = {}
    _pattern = re.compile(
        r'''\b(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'''
        r'''(?:25[0-5]|2[0-4]\d|[01]?\d\d?))(?=.*?(?:(?:(?:(?:25'''
        r'''[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'''
        r''')|(?P<port>\d{2,5})))''',
        flags=re.DOTALL,
    )

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(":"\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        tpl = 'http://www.nntime.com/proxy-updated-{:02}.htm'
        urls = [tpl.format(n) for n in range(1, 31)]
        await self._find_on_pages(urls)


class Spys_ru(Provider):
    domain = 'spys.ru'
    charEqNum = {}

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0].split('+')
        # ex: '+(i9w3m3^k1y5)+(g7g7g7^v2e5)+(d4r8o5^i9u1)+(y5c3e5^t0z6)'
        # => ['', '(i9w3m3^k1y5)', '(g7g7g7^v2e5)',
        #     '(d4r8o5^i9u1)', '(y5c3e5^t0z6)']
        # => ['i9w3m3', 'k1y5'] => int^int
        num = ''
        for numOfChars in chars[1:]:  # first - is ''
            var1, var2 = numOfChars.strip('()').split('^')
            digit = self.charEqNum[var1] ^ self.charEqNum[var2]
            num += str(digit)
        return num

    def find_proxies(self, page):
        expPortOnJS = r'(?P<js_port_code>(?:\+\([a-z0-9^+]+\))+)'
        # expCharNum = r'\b(?P<char>[a-z\d]+)=(?P<num>[a-z\d\^]+);'
        expCharNum = r'[>;]{1}(?P<char>[a-z\d]{4,})=(?P<num>[a-z\d\^]+)'
        # self.charEqNum = {
        #     char: i for char, i in re.findall(expCharNum, page)}
        res = re.findall(expCharNum, page)
        for char, num in res:
            if '^' in num:
                digit, tochar = num.split('^')
                num = int(digit) ^ self.charEqNum[tochar]
            self.charEqNum[char] = int(num)
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        expSession = r"'([a-z0-9]{32})'"
        url = 'http://spys.one/proxies/'
        page = await self.get(url)
        if not page:
            return
        sessionId = re.findall(expSession, page)[0]
        data = {
            'xx0': sessionId,  # session id
            'xpp': 5,  # 5 - 500 proxies on page
            'xf1': None,
        }  # 1 = ANM & HIA; 3 = ANM; 4 = HIA
        method = 'POST'
        urls = [
            {'url': url, 'data': {**data, 'xf1': lvl}, 'method': method}
            for lvl in [3, 4]
        ]
        await self._find_on_pages(urls)
        # expCountries = r'>([A-Z]{2})<'
        # url = 'http://spys.ru/proxys/'
        # page = await self.get(url)
        # links = ['http://spys.ru/proxys/%s/' %
        #          isoCode for isoCode in re.findall(expCountries, page)]


class My_proxy_com(Provider):
    domain = 'my-proxy.com'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?free-[^'"]*)['"]'''
        url = 'https://www.my-proxy.com/free-proxy-list.html'
        page = await self.get(url)
        if not page:
            return
        urls = [
            'https://www.my-proxy.com/%s' % path
            for path in re.findall(exp, page)
        ]
        urls.append(url)
        await self._find_on_pages(urls)


class Proxylist_download(Provider):
    domain = 'www.proxy-list.download'

    async def _pipe(self):
        urls = [
            'https://www.proxy-list.download/api/v1/get?type=http',
            'https://www.proxy-list.download/api/v1/get?type=https',
            'https://www.proxy-list.download/api/v1/get?type=socks4',
            'https://www.proxy-list.download/api/v1/get?type=socks5',
        ]
        await self._find_on_pages(urls)


class ProxyProvider(Provider):
    def __init__(self, *args, **kwargs):
        warnings.warn(
            '`ProxyProvider` is deprecated, use `Provider` instead.',
            DeprecationWarning,
        )
        super().__init__(*args, **kwargs)


class FileProvider(Provider):
    async def _pipe(self):
        with open(self.url, 'r') as f:
            self.proxies = self.find_proxies(f.read())


PROVIDERS = [
    proxyservers_pro(),
    
    Provider(
        url=f'https://checkerproxy.net/api/archive/{(datetime.now() - timedelta(days=1)).date()}',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
	),
    Provider(
        url='https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://openproxy.space/list/socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://www.proxy-list.download/api/v1/get?type=socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://www.proxyscan.io/download?type=socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-socks-4-proxy.html',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://www.socks-proxy.net/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.freeproxychecker.com/result/socks4_proxies.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://github.com/ShiftyTR/Proxy-List/blob/master/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='http://proxydb.net/?protocol=socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://api.openproxylist.xyz/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://openproxy.space/list/socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/manuGMG/proxy-365/main/SOCKS5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://www.proxyscan.io/download?type=socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://spys.me/socks.txt',
        proto=('SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-socks-5-proxy.html',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://github.com/ShiftyTR/Proxy-List/blob/master/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='http://proxydb.net/?protocol=socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://www.proxy-list.download/api/v1/get?type=socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://api.openproxylist.xyz/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://api.proxyscrape.com/v2/?request=getproxies&protocol=http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://openproxy.space/list/http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/almroot/proxylist/master/list.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http%2Bhttps.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/User-R3X/proxy-list/main/online/all.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.proxy-list.download/api/v1/get?type=http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://www.proxy-list.download/api/v1/get?type=https',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='http://spys.me/proxy.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.sslproxies.org/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-2.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-3.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-4.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-5.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-6.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-7.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-8.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-9.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.my-proxy.com/free-proxy-list-10.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://github.com/ShiftyTR/Proxy-List/blob/master/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://github.com/ShiftyTR/Proxy-List/blob/master/https.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='http://proxydb.net/?protocol=http&protocol=https',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://www.proxyscan.io/download?type=http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://api.openproxylist.xyz/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://api.proxyscrape.com/?request=getproxies&proxytype=http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://api.proxyscrape.com/?request=getproxies&proxytype=socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://api.proxyscrape.com/?request=getproxies&proxytype=socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='http://ipaddress.com/proxy-list/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://freshfreeproxylist.wordpress.com/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='http://proxytime.ru/http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://free-proxy-list.net/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://socks-proxy.net/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='http://www.httptunnel.ge/ProxyListForFree.aspx',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='http://cn-proxy.com/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://hugeproxies.com/home/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=20&format=txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
        max_conn=1,
    ),
	Provider(
        url='https://multiproxy.org/txt_all/proxy.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='http://www.proxylists.net/http_highanon.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://pastebin.com/raw/vQzZ8CwG',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/human1ty/proxy/main/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/human1ty/proxy/main/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/human1ty/proxy/main/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://github.com/KarboDuck/mhddos_bash/raw/main/final.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=2&format=txt&type=socks5',
        max_conn=1,
        proto=('SOCKS5'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=2&format=txt&type=socks4',
        max_conn=1,
        proto=('SOCKS4'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=2&format=txt&http=true&type=http',
        max_conn=1,
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=2&format=txt&country=RU&type=socks5',
        max_conn=1,
        proto=('SOCKS5'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=2&format=txt&country=RU&type=socks4',
        max_conn=1,
        proto=('SOCKS4'),
    ),
	Provider(
        url='http://pubproxy.com/api/proxy?limit=2&format=txt&country=RU&http=true&type=http',
        max_conn=1,
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://premiumproxy.net/full-proxy-list',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://proxypremium.top/full-proxy-list',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://pastebin.com/raw/vLN81LDa',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://pastebin.com/raw/1DeAN3xi',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/mmpx12/proxy-list/master/proxies.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://spys.me/socks.txt',
        proto=('SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='http://spys.me/proxy.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.freeproxychecker.com/result/mixed_proxies.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=10000&country=all',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000&country=all',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/proxy.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.sslproxies.org/#raw',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://t.me/s/proxiesfine',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://free-proxy-list.net/anonymous-proxy.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://free-proxy-list.net/uk-proxy.html',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://www.us-proxy.org/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://socks-proxy.net/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://proxy-list.org/russian/search.php?search=RU&country=RU&type=any&port=any&ssl=any',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
	Provider(
        url='https://api.best-proxies.ru/proxylist.txt?key=developer&type=http',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://api.best-proxies.ru/proxylist.txt?key=developer&type=https',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://api.best-proxies.ru/proxylist.txt?key=developer&type=socks4',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://api.best-proxies.ru/proxylist.txt?key=developer&type=socks5',
        proto=('SOCKS5'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
        proto=('HTTP', 'HTTPS'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt',
        proto=('SOCKS4'),
    ),
	Provider(
        url='https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt',
        proto=('SOCKS5'),
    ),
#uashield
	Provider(
        url='https://raw.githubusercontent.com/opengs/uashieldtargets/master/proxy.json',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),
#mhddos 
	Provider(
        url='https://raw.githubusercontent.com/porthole-ascend-cinnamon/proxy_scraper/main/proxies.txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25', 'SOCKS4', 'SOCKS5'),
    ),

    Proxy_list_org(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')
    ),  # noqa; 140
    Xseo_in(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 240
    Spys_ru(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 660
    Foxtools_ru(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'), max_conn=1
    ),  # noqa; 500
    Nntime_com(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')
    ),  # noqa; 1050
    Blogspot_com(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')
    ),  # noqa; 24800
    My_proxy_com(max_conn=2),  # noqa; 1000
    Checkerproxy_net(),  # noqa; 60000
    Webanetlabs_net(),  # noqa; 5000
    Proxylist_download()
]
