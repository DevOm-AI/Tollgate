from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

# Paths that answer in OpenAI's error format, so OpenAI client libraries can read the errors.
OPENAI_PATH_PREFIX = "/v1/"


class OpenAIError(Exception):
    """An error answered as {"error": {"message", "type", "param", "code"}}, like OpenAI's API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type
        self.code = code
        self.param = param
        self.headers = headers

    def response(self) -> JSONResponse:
        body = {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }
        return JSONResponse(body, status_code=self.status_code, headers=self.headers)


async def _openai_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, OpenAIError)
    return exc.response()


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    if not request.url.path.startswith(OPENAI_PATH_PREFIX):
        return await request_validation_exception_handler(request, exc)
    # Only the location and the reason: the rejected input may be part of a prompt.
    first = exc.errors()[0]
    location = [str(part) for part in first["loc"] if part != "body"]
    param = ".".join(location) or None
    message = f"{param}: {first['msg']}" if param else first["msg"]
    return OpenAIError(status.HTTP_400_BAD_REQUEST, message, param=param).response()


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(OpenAIError, _openai_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
