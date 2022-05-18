"""
Ref:
- https://en.wikipedia.org/wiki/Basic_access_authentication
- https://en.wikipedia.org/wiki/Digest_access_authentication
- https://datatracker.ietf.org/doc/html/rfc2617
- https://datatracker.ietf.org/doc/html/rfc7616
- https://datatracker.ietf.org/doc/html/rfc7617
- https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Authentication
- https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Headers/Digest

- https://github.com/dimagi/python-digest/blob/master/python_digest/__init__.py
- https://github.com/psf/requests/blob/master/requests/auth.py
- https://gist.github.com/dayflower/5828503
"""
import re
from typing import Optional
from base64 import b64encode
from uuid import uuid4
from hashlib import md5
from logging import getLogger

from asgi_webdav.constants import DAVUser
from asgi_webdav.config import Config
from asgi_webdav.request import DAVRequest
from asgi_webdav.response import DAVResponse


logger = getLogger(__name__)


class HTTPAuthAbc:
    def __init__(self, realm: str):
        self.realm = realm

    @staticmethod
    def is_credential(authorization_header: bytes) -> bool:
        raise NotImplementedError

    def make_auth_challenge_string(self) -> bytes:
        raise NotImplementedError


class HTTPBasicAuth(HTTPAuthAbc):
    credential_user_mapping: dict[bytes, DAVUser] = dict()  # basic string: DAVUser

    def __init__(self, realm: str, user_mapping: dict[str, DAVUser]):
        super().__init__(realm=realm)

        for user in user_mapping.values():
            basic_credential = b64encode(
                f"{user.username}:{user.password}".encode("utf-8")
            )

            self.credential_user_mapping[basic_credential] = user

    @staticmethod
    def is_credential(authorization_header: bytes) -> bool:
        return authorization_header[:6].lower() == b"basic "

    def make_auth_challenge_string(self) -> bytes:
        return f'Basic realm="{self.realm}"'.encode("utf-8")

    def verify_user(self, authorization_header: bytes) -> Optional[DAVUser]:
        return self.credential_user_mapping.get(authorization_header[6:])


DIGEST_AUTHORIZATION_PARAMS = {
    "username",
    "realm",
    "nonce",
    "uri",
    "response",
    "algorithm",
    "opaque",
    "qop",
    "nc",
    "cnonce",
}


class HTTPDigestAuth(HTTPAuthAbc):
    # https://datatracker.ietf.org/doc/html/rfc7616#section-3.5
    # The Authentication-Info header field is allowed in the trailer of an
    # HTTP message transferred via chunked transfer coding.
    #
    # For historical reasons, a sender MUST only generate the quoted string
    # syntax for the following parameters: nextnonce, rspauth, and cnonce.
    #
    # For historical reasons, a sender MUST NOT generate the quoted string
    # syntax for the following parameters: qop and nc.
    #
    # For historical reasons, the nc value MUST be exactly 8 hexadecimal
    # digits.

    # http://www.webdav.org/neon/doc/html/compliance.html#idm140606748304208
    # neon is not strictly compliant with the quoting rules given in the grammar for
    # the Authorization header. The grammar requires that the qop and algorithm
    # parameters are not quoted, however one widely deployed server implementation
    # (Microsoft® IIS 5) rejects the request if these parameters are not quoted. neon
    # sends these parameters with quotes—this is not known to cause any problems with
    # other server implementations.

    def __init__(self, realm: str, secret: Optional[str] = None):
        super().__init__(realm=realm)

        self.secret = uuid4().hex if secret is None else secret
        self.opaque = uuid4().hex.upper()

    @staticmethod
    def is_credential(authorization_header: bytes) -> bool:
        return authorization_header[:7].lower() == b"digest "

    def make_auth_challenge_string(self) -> bytes:
        return "Digest {}".format(
            self.authorization_string_build_from_data(
                {
                    "realm": self.realm,
                    "qop": "auth",
                    "nonce": self.nonce,
                    "opaque": self.opaque,
                    "algorithm": "MD5",
                    "stale": "false",
                }
            )
        ).encode("utf-8")
        # f_str = (
        #     'Digest realm="{}", nonce="{}", opaque="{}",'
        #     ' qop=auth, algorithm=MD5, stale="false"'
        # )
        # return f_str.format(self.realm, self.nonce, self.opaque).encode("utf-8")
        # f_str = 'Digest realm="{}", nonce="{}",
        # opaque="{}", qop="auth", algorithm=MD5'
        # return f_str.format(self.realm, self.nonce, self.opaque).encode("utf-8")

    def make_response_authentication_info_string(
        self,
        request: DAVRequest,
        user: DAVUser,
        digest_auth_data: dict[str, str],
    ) -> bytes:
        ha1, ha2 = self.build_ha1_ha2_digest(
            username=user.username,
            password=user.password,
            method=request.method,
            uri=digest_auth_data.get("uri"),  # TODO!!!,
        )
        rspauth = self.build_md5_digest(
            [
                ha1,
                digest_auth_data.get("nonce"),
                digest_auth_data.get("nc"),
                digest_auth_data.get("cnonce"),
                digest_auth_data.get("qop"),
                ha2,
            ]
        )
        return self.authorization_string_build_from_data(
            {
                "rspauth": rspauth,
                "qop": digest_auth_data.get("qop"),
                "cnonce": digest_auth_data.get("cnonce"),
                "nc": digest_auth_data.get("nc"),
            }
        ).encode("utf-8")
        # return 'rspauth="{}", cnonce="{}", qop={}, nc={}'.format(
        #     rspauth,
        #     digest_auth_data.get("cnonce"),
        #     digest_auth_data.get("qop"),
        #     digest_auth_data.get("nc"),
        # ).encode("utf-8")

    @property
    def nonce(self) -> str:
        return md5(f"{uuid4().hex}{self.secret}".encode("utf-8")).hexdigest()

    @staticmethod
    def authorization_str_parser_to_data(authorization: str) -> dict:
        values = authorization.split(",")
        data = {}
        for value in values:
            try:
                k, v = value.split("=", maxsplit=1)
                k = k.strip(" ").rstrip(" ")
                v = v.strip(' "').rstrip(' "').strip("'").rstrip("'")
                data[k] = v
            except ValueError as e:
                logger.error(f"parser:{value} failed, ", e)

        return data

    @staticmethod
    def authorization_string_build_from_data(data: dict[str, str]) -> str:
        return ", ".join(['%s="%s"' % (k, v) for (k, v) in data.items()])

    @staticmethod
    def build_md5_digest(data: list[str]) -> str:
        return md5(":".join(data).encode("utf-8")).hexdigest()

    def build_ha1_ha2_digest(
        self, username: str, password: str, method: str, uri: str
    ) -> (str, str):
        # HA1 = MD5(username:realm:password)
        ha1 = self.build_md5_digest([username, self.realm, password])

        # HA2 = MD5(method:digestURI)
        ha2 = self.build_md5_digest([method, uri])

        return ha1, ha2

    def build_request_digest(
        self,
        request: DAVRequest,
        user: DAVUser,
        digest_auth_data: dict[str, str],
    ) -> str:
        ha1, ha2 = self.build_ha1_ha2_digest(
            username=user.username,
            password=user.password,
            method=request.method,
            uri=digest_auth_data.get("uri"),  # TODO!!!,
        )

        if digest_auth_data.get("qop") == "auth":
            # MD5(HA1:nonce:nonceCount:cnonce:qop:HA2)
            return self.build_md5_digest(
                [
                    ha1,
                    digest_auth_data.get("nonce"),
                    digest_auth_data.get("nc"),
                    digest_auth_data.get("cnonce"),
                    digest_auth_data.get("qop"),
                    ha2,
                ]
            )

        # MD5(HA1:nonce:HA2)
        return self.build_md5_digest(
            [
                ha1,
                digest_auth_data.get("nonce"),
                ha2,
            ]
        )


MESSAGE_401_TEMPLATE = """<!DOCTYPE html>
<html>
  <head>
    <meta charset="UTF-8" />
    <title>Error</title>
  </head>
  <body>
    <h1>401 Unauthorized. {}</h1>
  </body>
</html>"""


class DAVAuth:
    realm = "ASGI-WebDAV"
    user_mapping: dict[str, DAVUser] = dict()  # username: password

    def __init__(self, config: Config):
        self.config = config

        for config_account in config.account_mapping:
            user = DAVUser(
                username=config_account.username,
                password=config_account.password,
                permissions=config_account.permissions,
                admin=config_account.admin,
            )

            self.user_mapping[config_account.username] = user
            logger.info(f"Register User: {user}")

        self.basic_auth = HTTPBasicAuth(
            realm=self.realm, user_mapping=self.user_mapping
        )
        self.digest_auth = HTTPDigestAuth(realm=self.realm, secret=uuid4().hex)

    def pick_out_user(self, request: DAVRequest) -> (Optional[DAVUser], str):
        authorization_header = request.headers.get(b"authorization")
        if authorization_header is None:
            return None, "miss header: authorization"

        # Basic
        if self.basic_auth.is_credential(authorization_header):
            request.authorization_method = "Basic"

            user = self.basic_auth.verify_user(authorization_header)
            if user is None:
                return None, "no permission"

            return user, ""

        # Digest
        if self.digest_auth.is_credential(authorization_header):
            request.authorization_method = "Digest"

            digest_auth_data = self.digest_auth.authorization_str_parser_to_data(
                (authorization_header[7:].decode("utf-8"))
            )
            if len(DIGEST_AUTHORIZATION_PARAMS - set(digest_auth_data.keys())) > 0:
                return None, "no permission"

            user = self.user_mapping.get(digest_auth_data.get("username"))
            if user is None:
                return None, "no permission"

            expected_request_digest = self.digest_auth.build_request_digest(
                request=request,
                user=user,
                digest_auth_data=digest_auth_data,
            )
            request_digest = digest_auth_data.get("response")
            if expected_request_digest != request_digest:
                logger.debug(
                    f"expected_request_digest:{expected_request_digest},"
                    f" but request_digest:{request_digest}"
                )
                return None, "no permission"

            # https://datatracker.ietf.org/doc/html/rfc2617#page-15
            # macOS 11.4 finder supported
            #   WebDAVFS/3.0.0 (03008000) Darwin/20.5.0 (x86_64)
            request.authorization_info = (
                self.digest_auth.make_response_authentication_info_string(
                    request=request,
                    user=user,
                    digest_auth_data=digest_auth_data,
                )
            )
            return user, ""

        return None, "Unknown authentication method"

    def create_response_401(self, request: DAVRequest, message: str) -> DAVResponse:
        if self.config.http_digest_auth.enable:
            enable_digest = not self._match_user_agent(
                rule=self.config.http_digest_auth.disable_rule,
                user_agent=request.client_user_agent,
            )

        else:
            enable_digest = bool(
                self._match_user_agent(
                    rule=self.config.http_digest_auth.enable_rule,
                    user_agent=request.client_user_agent,
                )
            )

        if enable_digest:
            challenge_string = self.digest_auth.make_auth_challenge_string()
            logger.debug("response Digest auth challenge")
        else:
            challenge_string = self.basic_auth.make_auth_challenge_string()
            logger.debug("response Basic auth challenge")

        return DAVResponse(
            status=401,
            content=MESSAGE_401_TEMPLATE.format(message).encode("utf-8"),
            headers={b"WWW-Authenticate": challenge_string},
        )

    @staticmethod
    def _match_user_agent(rule: str, user_agent: str) -> bool:
        return re.match(rule, user_agent) is not None

    @staticmethod
    def _parser_digest_request(authorization: str) -> dict:
        values = authorization[7:].split(",")

        data = {}
        for value in values:
            value = value.replace('"', "").replace(" ", "")
            try:
                k, v = value.split("=")
                data[k] = v
            except ValueError:
                pass

        return data
