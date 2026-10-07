"""Exception hierarchy.

Every exception raised across a layer boundary is an :class:`AnkerError` with an
:class:`~anker_client.core.models.ErrorKind` and a message that is safe to show
to users. Low-level exceptions (``requests``, ``OSError``…) must be wrapped at
the boundary that understands them (``raise NetworkError(...) from exc``).
"""

from __future__ import annotations

from anker_client.core.models import ErrorKind


class AnkerError(Exception):
    kind: ErrorKind = ErrorKind.UNKNOWN
    #: Whether retrying the same operation later can reasonably succeed.
    retryable: bool = False

    def __init__(self, message: str = "", *, detail: str = "") -> None:
        super().__init__(message or self.default_message())
        self.message = message or self.default_message()
        #: Extra technical information for logs (never required for the UI).
        self.detail = detail

    @classmethod
    def default_message(cls) -> str:
        return "Something went wrong."

    @property
    def user_message(self) -> str:
        return self.message


# --- network / site ---------------------------------------------------------


class NetworkError(AnkerError):
    kind = ErrorKind.NETWORK
    retryable = True

    def __init__(self, message: str = "", *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message, detail=detail)
        self.status = status

    @classmethod
    def default_message(cls) -> str:
        return "Could not reach AnkerGames. Check your internet connection."


class NotFoundError(AnkerError):
    kind = ErrorKind.NOT_FOUND

    @classmethod
    def default_message(cls) -> str:
        return "The requested page no longer exists on AnkerGames."


class SiteChangedError(AnkerError):
    """The site's markup/API no longer matches what the parser expects."""

    kind = ErrorKind.SITE_CHANGED

    @classmethod
    def default_message(cls) -> str:
        return "AnkerGames changed its website and this feature needs an AnkerClient update."


class RateLimitedError(AnkerError):
    kind = ErrorKind.RATE_LIMITED
    retryable = True

    def __init__(self, retry_after: int = 60, message: str = "", *, detail: str = "") -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__(message or f"Too many requests. Try again in {self.retry_after} seconds.", detail=detail)


class QuotaExceededError(AnkerError):
    """The account/guest download quota is used up (site's ``show_upgrade``)."""

    kind = ErrorKind.QUOTA

    @classmethod
    def default_message(cls) -> str:
        return "Your AnkerGames download limit has been reached. Sign in or try again later."


class GeoBlockedError(AnkerError):
    kind = ErrorKind.GEO_BLOCKED

    @classmethod
    def default_message(cls) -> str:
        return "This download is not available in your region."


class AccessDeniedError(AnkerError):
    """Subscription-only or sign-in-only content."""

    kind = ErrorKind.ACCESS_DENIED

    @classmethod
    def default_message(cls) -> str:
        return "This download requires an AnkerGames account or subscription."


class LinkExpiredError(AnkerError):
    """A resolved download URL (or ticket) is no longer valid — re-resolve it."""

    kind = ErrorKind.LINK_EXPIRED
    retryable = True

    @classmethod
    def default_message(cls) -> str:
        return "The download link expired."


# --- verification -----------------------------------------------------------


class VerificationError(AnkerError):
    kind = ErrorKind.VERIFICATION
    retryable = True

    def __init__(self, message: str = "", *, ticket_url: str = "", detail: str = "") -> None:
        super().__init__(message, detail=detail)
        self.ticket_url = ticket_url

    @classmethod
    def default_message(cls) -> str:
        return "The download could not be verified."


class VerificationTimeout(VerificationError):
    @classmethod
    def default_message(cls) -> str:
        return "Verification timed out. Retry, or open the download page in your browser."


class VerificationCancelled(VerificationError):
    retryable = False

    @classmethod
    def default_message(cls) -> str:
        return "Verification was cancelled."


class VerificationUnavailable(VerificationError):
    """No embedded browser is available to run the site's challenge."""

    retryable = False

    @classmethod
    def default_message(cls) -> str:
        return (
            "This download needs a browser check that AnkerClient cannot show on this system. "
            "Open it in your browser, then import the downloaded archive."
        )


class ExternalHostError(AnkerError):
    """The site hands this download off to an external file host."""

    kind = ErrorKind.EXTERNAL_HOST

    def __init__(self, url: str, provider: str = "", *, detail: str = "") -> None:
        self.url = url
        self.provider = provider
        name = provider or "an external file host"
        super().__init__(
            f"This download is hosted on {name}. Open it in your browser, then import the archive.",
            detail=detail,
        )


# --- auth -------------------------------------------------------------------


class AuthError(AnkerError):
    kind = ErrorKind.AUTH

    @classmethod
    def default_message(cls) -> str:
        return "Sign-in failed."


class LoginFailedError(AuthError):
    @classmethod
    def default_message(cls) -> str:
        return "Incorrect email or password."


class NotLoggedInError(AuthError):
    @classmethod
    def default_message(cls) -> str:
        return "You need to sign in to AnkerGames for this."


# --- local machine ----------------------------------------------------------


class DiskSpaceError(AnkerError):
    kind = ErrorKind.DISK_SPACE

    def __init__(self, required: int, available: int, path: str, *, detail: str = "") -> None:
        from anker_client.core.formatting import format_bytes

        self.required = required
        self.available = available
        self.path = path
        super().__init__(
            f"Not enough disk space on {path}: {format_bytes(required)} needed, "
            f"{format_bytes(available)} free.",
            detail=detail,
        )


class DownloadError(AnkerError):
    kind = ErrorKind.NETWORK
    retryable = True

    @classmethod
    def default_message(cls) -> str:
        return "The download failed."


class IntegrityError(DownloadError):
    """Downloaded bytes do not match what the server announced."""

    @classmethod
    def default_message(cls) -> str:
        return "The downloaded file is incomplete or corrupt."


class ExtractionError(AnkerError):
    kind = ErrorKind.EXTRACTION

    @classmethod
    def default_message(cls) -> str:
        return "The archive could not be extracted."


class SevenZipNotFoundError(ExtractionError):
    @classmethod
    def default_message(cls) -> str:
        return "7-Zip was not found. Install 7-Zip or set its location in Settings."


class PasswordProtectedArchiveError(ExtractionError):
    @classmethod
    def default_message(cls) -> str:
        return "The archive is password protected."


class CorruptArchiveError(ExtractionError):
    retryable = True

    @classmethod
    def default_message(cls) -> str:
        return "The archive is damaged. Delete it and download again."


class InstallError(AnkerError):
    kind = ErrorKind.INSTALL

    @classmethod
    def default_message(cls) -> str:
        return "The game could not be installed."


class LaunchError(AnkerError):
    kind = ErrorKind.UNKNOWN

    @classmethod
    def default_message(cls) -> str:
        return "The game could not be started."


class ExecutableNotSetError(LaunchError):
    @classmethod
    def default_message(cls) -> str:
        return "Choose which program starts this game."


class OperationCancelled(AnkerError):
    """Raised by long operations when their :class:`CancelToken` fires."""

    kind = ErrorKind.CANCELLED

    @classmethod
    def default_message(cls) -> str:
        return "Cancelled."
