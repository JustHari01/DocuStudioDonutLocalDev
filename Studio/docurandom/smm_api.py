from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .donut_mcp import _protect_bytes, _unprotect_bytes
from .storage import app_data_dir


ONE_MM_API_URL = "https://1mm.me/api/v2"
ONE_MM_API_KEY_ENV = "DOCURANDOM_1MM_API_KEY"
_SECRET_FILENAME = "one_mm_api_key.dat"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class SmmApiError(RuntimeError):
    """Base error for a rejected or unreadable 1MM API request."""


class SmmProviderRejectedError(SmmApiError):
    """1MM explicitly rejected the request and did not create an order."""


class SmmAuthenticationError(SmmApiError):
    """The API key is missing or was rejected by 1MM."""


class SmmTransportError(SmmApiError):
    """The request could not reach 1MM safely."""


class SmmResponseError(SmmApiError):
    """1MM returned a malformed or unsupported response."""


@dataclass(frozen=True)
class SmmService:
    service_id: str
    name: str
    service_type: str
    category: str
    rate: Decimal
    minimum: int
    maximum: int
    refill: bool


@dataclass(frozen=True)
class SmmProduct:
    product_id: str
    name: str
    category: str
    rate: Decimal
    minimum: int
    maximum: int
    status: str
    inventory: int | None


@dataclass(frozen=True)
class SmmBalance:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class SmmOrderStatus:
    order_id: str
    status: str
    charge: Decimal | None = None
    start_count: int | None = None
    remains: int | None = None
    error: str = ""


@dataclass(frozen=True)
class SmmProductOrderStatus:
    order_id: str
    status: str
    charge: Decimal | None = None
    result: object | None = None


def _secret_path(path: Path | None = None) -> Path:
    return path or app_data_dir() / "secrets" / _SECRET_FILENAME


def _normalize_api_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise SmmAuthenticationError("Chưa nhập API key 1MM.")
    if len(key) > 512 or any(ord(character) < 33 for character in key):
        raise SmmAuthenticationError("API key 1MM không hợp lệ.")
    return key


def save_smm_api_key(value: str, path: Path | None = None) -> None:
    """Store the key with Windows DPAPI; plaintext is never written to disk."""

    key = _normalize_api_key(value)
    target = _secret_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_protect_bytes(key.encode("utf-8")))
        os.replace(temporary, target)
    except SmmApiError:
        raise
    except Exception as exc:
        raise SmmApiError("Không thể lưu API key 1MM vào kho mã hóa Windows.") from exc
    finally:
        temporary.unlink(missing_ok=True)


def load_smm_api_key(path: Path | None = None) -> str | None:
    target = _secret_path(path)
    try:
        protected = target.read_bytes()
    except OSError:
        protected = b""
    if protected:
        try:
            return _normalize_api_key(_unprotect_bytes(protected).decode("utf-8"))
        except Exception:
            return None
    environment_key = os.environ.get(ONE_MM_API_KEY_ENV, "")
    try:
        return _normalize_api_key(environment_key) if environment_key else None
    except SmmAuthenticationError:
        return None


def clear_smm_api_key(path: Path | None = None) -> None:
    try:
        _secret_path(path).unlink(missing_ok=True)
    except OSError as exc:
        raise SmmApiError("Không thể xóa API key 1MM đã lưu.") from exc


def _decimal(value: object, *, field: str, optional: bool = False) -> Decimal | None:
    if value is None or value == "":
        if optional:
            return None
        raise SmmResponseError(f"Phản hồi 1MM thiếu trường {field}.")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SmmResponseError(f"Phản hồi 1MM có trường {field} không hợp lệ.") from exc


def _integer(value: object, *, field: str, optional: bool = False) -> int | None:
    if value is None or value == "":
        if optional:
            return None
        raise SmmResponseError(f"Phản hồi 1MM thiếu trường {field}.")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise SmmResponseError(f"Phản hồi 1MM có trường {field} không hợp lệ.") from exc


def _identifier(value: object, *, field: str) -> str:
    identifier = str(value if value is not None else "").strip()
    if not identifier:
        raise SmmResponseError(f"Phản hồi 1MM thiếu trường {field}.")
    return identifier


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _safe_api_message(value: object) -> str:
    message = " ".join(str(value or "").split())[:500]
    return message or "1MM từ chối yêu cầu."


class SmmApiClient:
    """Typed client for all endpoints documented by the 1MM API v2."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = ONE_MM_API_URL,
        timeout: float = 30.0,
        opener: Callable[..., object] = urlopen,
        key_loader: Callable[[], str | None] = load_smm_api_key,
    ) -> None:
        if base_url != ONE_MM_API_URL and not base_url.startswith("https://"):
            raise ValueError("Endpoint 1MM phải dùng HTTPS.")
        self._api_key = _normalize_api_key(api_key) if api_key else None
        self.base_url = base_url
        self.timeout = max(1.0, float(timeout))
        self._opener = opener
        self._key_loader = key_loader

    def _resolved_key(self) -> str:
        key = self._api_key or self._key_loader()
        if key is None:
            raise SmmAuthenticationError("Chưa lưu API key 1MM.")
        return _normalize_api_key(key)

    def _request(self, action: str, **parameters: object) -> object:
        form: dict[str, str] = {
            "key": self._resolved_key(),
            "action": str(action),
        }
        for name, value in parameters.items():
            if value is not None:
                form[str(name)] = str(value)
        body = urlencode(form).encode("utf-8")
        request = Request(
            self.base_url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "DocuRandomStudio/1MM",
            },
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            with response:  # type: ignore[attr-defined]
                raw = response.read(_MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
        except HTTPError as exc:
            raise SmmTransportError(f"1MM trả về lỗi HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise SmmTransportError("Không thể kết nối API 1MM. Hãy kiểm tra mạng rồi thử lại.") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise SmmResponseError("Phản hồi 1MM vượt quá giới hạn an toàn.")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SmmResponseError("1MM không trả về JSON hợp lệ.") from exc
        if isinstance(payload, Mapping) and "error" in payload:
            message = _safe_api_message(payload.get("error"))
            if "key" in message.casefold() or "auth" in message.casefold():
                raise SmmAuthenticationError(message)
            raise SmmProviderRejectedError(message)
        return payload

    def services(self) -> tuple[SmmService, ...]:
        payload = self._request("services")
        if not isinstance(payload, list):
            raise SmmResponseError("Danh sách dịch vụ 1MM không đúng cấu trúc.")
        services: list[SmmService] = []
        for item in payload:
            if not isinstance(item, Mapping):
                raise SmmResponseError("Một dịch vụ 1MM không đúng cấu trúc.")
            minimum = _integer(item.get("min"), field="min")
            maximum = _integer(item.get("max"), field="max")
            assert minimum is not None and maximum is not None
            if minimum < 0 or maximum < minimum:
                raise SmmResponseError("Giới hạn số lượng dịch vụ 1MM không hợp lệ.")
            rate = _decimal(item.get("rate"), field="rate")
            assert rate is not None
            services.append(
                SmmService(
                    service_id=_identifier(item.get("service"), field="service"),
                    name=str(item.get("name") or "Dịch vụ không tên").strip(),
                    service_type=str(item.get("type") or "Default").strip(),
                    category=str(item.get("category") or "Khác").strip(),
                    rate=rate,
                    minimum=minimum,
                    maximum=maximum,
                    refill=_truthy(item.get("refill")),
                )
            )
        return tuple(services)

    def add_order(
        self,
        service_id: str | int,
        link: str,
        *,
        quantity: int | None = None,
        comments: str | None = None,
    ) -> str:
        target = str(link or "").strip()
        if not target:
            raise ValueError("Đơn SMM cần URL đích.")
        if quantity is None and not str(comments or "").strip():
            raise ValueError("Đơn SMM cần số lượng hoặc danh sách bình luận.")
        if quantity is not None and int(quantity) <= 0:
            raise ValueError("Số lượng đơn SMM phải lớn hơn 0.")
        payload = self._request(
            "add",
            service=_identifier(service_id, field="service"),
            link=target,
            quantity=int(quantity) if quantity is not None else None,
            comments=str(comments) if comments is not None else None,
        )
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Kết quả tạo đơn 1MM không đúng cấu trúc.")
        return _identifier(payload.get("order"), field="order")

    @staticmethod
    def _parse_order_status(order_id: str, payload: object) -> SmmOrderStatus:
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Trạng thái đơn 1MM không đúng cấu trúc.")
        if "error" in payload:
            return SmmOrderStatus(
                order_id=order_id,
                status="Error",
                error=_safe_api_message(payload.get("error")),
            )
        return SmmOrderStatus(
            order_id=order_id,
            status=str(payload.get("status") or "Unknown").strip(),
            charge=_decimal(payload.get("charge"), field="charge", optional=True),
            start_count=_integer(
                payload.get("start_count"), field="start_count", optional=True
            ),
            remains=_integer(payload.get("remains"), field="remains", optional=True),
        )

    def order_status(self, order_id: str | int) -> SmmOrderStatus:
        identifier = _identifier(order_id, field="order")
        return self._parse_order_status(
            identifier,
            self._request("status", order=identifier),
        )

    def orders_status(
        self, order_ids: Iterable[str | int]
    ) -> dict[str, SmmOrderStatus]:
        identifiers = tuple(_identifier(value, field="order") for value in order_ids)
        if not identifiers:
            raise ValueError("Cần ít nhất một mã đơn 1MM.")
        if len(identifiers) > 100:
            raise ValueError("1MM chỉ cho kiểm tra tối đa 100 đơn mỗi lần.")
        payload = self._request("status", orders=",".join(identifiers))
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Trạng thái nhiều đơn 1MM không đúng cấu trúc.")
        return {
            identifier: self._parse_order_status(identifier, payload.get(identifier, {}))
            for identifier in identifiers
        }

    def create_refill(self, order_id: str | int) -> str:
        payload = self._request("refill", order=_identifier(order_id, field="order"))
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Kết quả refill 1MM không đúng cấu trúc.")
        return _identifier(payload.get("refill"), field="refill")

    def products(self) -> tuple[SmmProduct, ...]:
        payload = self._request("products")
        if not isinstance(payload, list):
            raise SmmResponseError("Danh sách sản phẩm 1MM không đúng cấu trúc.")
        products: list[SmmProduct] = []
        for item in payload:
            if not isinstance(item, Mapping):
                raise SmmResponseError("Một sản phẩm 1MM không đúng cấu trúc.")
            minimum = _integer(item.get("min"), field="min")
            maximum = _integer(item.get("max"), field="max")
            rate = _decimal(item.get("rate"), field="rate")
            assert minimum is not None and maximum is not None and rate is not None
            products.append(
                SmmProduct(
                    product_id=_identifier(item.get("product"), field="product"),
                    name=str(item.get("name") or "Sản phẩm không tên").strip(),
                    category=str(item.get("category") or "Khác").strip(),
                    rate=rate,
                    minimum=minimum,
                    maximum=maximum,
                    status=str(item.get("status") or "Unknown").strip(),
                    inventory=_integer(
                        item.get("inventory"), field="inventory", optional=True
                    ),
                )
            )
        return tuple(products)

    def add_product_order(
        self,
        product_id: str | int,
        quantity: int,
        *,
        required_information: str = "",
    ) -> str:
        if int(quantity) <= 0:
            raise ValueError("Số lượng sản phẩm phải lớn hơn 0.")
        payload = self._request(
            "add_product_order",
            product=_identifier(product_id, field="product"),
            quantity=int(quantity),
            require=str(required_information),
        )
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Kết quả mua sản phẩm 1MM không đúng cấu trúc.")
        return _identifier(payload.get("order"), field="order")

    def product_order_status(self, order_id: str | int) -> SmmProductOrderStatus:
        identifier = _identifier(order_id, field="order")
        payload = self._request("product_order_status", order=identifier)
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Trạng thái đơn sản phẩm 1MM không đúng cấu trúc.")
        return SmmProductOrderStatus(
            order_id=identifier,
            status=str(payload.get("status") or "Unknown").strip(),
            charge=_decimal(payload.get("charge"), field="charge", optional=True),
            result=payload.get("result"),
        )

    def product_result(self, order_id: str | int) -> tuple[str, ...]:
        payload = self._request(
            "result_product", order=_identifier(order_id, field="order")
        )
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Kết quả sản phẩm 1MM không đúng cấu trúc.")
        result = payload.get("result")
        if isinstance(result, list):
            return tuple(str(item) for item in result)
        if result is None:
            return ()
        return (str(result),)

    def balance(self) -> SmmBalance:
        payload = self._request("balance")
        if not isinstance(payload, Mapping):
            raise SmmResponseError("Số dư 1MM không đúng cấu trúc.")
        amount = _decimal(payload.get("balance"), field="balance")
        assert amount is not None
        return SmmBalance(
            amount=amount,
            currency=str(payload.get("currency") or "USD").strip().upper(),
        )
