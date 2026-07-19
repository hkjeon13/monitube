"""Browser-session authentication routes."""

from fastapi import APIRouter, HTTPException, Request, Response, status

from ...contracts import AuthUserResponse, LoginRequest
from ...settings import Settings
from ..dependencies import User, set_session_cookie


def create_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["auth"])

    @router.get("/me", response_model=AuthUserResponse)
    def current_user(user: User) -> AuthUserResponse:
        return AuthUserResponse(username=user.username)

    @router.post(
        "/register",
        response_model=AuthUserResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def register(
        payload: LoginRequest,
        request: Request,
        response: Response,
    ) -> AuthUserResponse:
        auth_store = request.app.state.auth_store
        if auth_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="계정 저장소를 사용할 수 없습니다.",
            )
        try:
            user = auth_store.register(payload.username, payload.password)
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="이미 사용 중인 아이디입니다.",
                ) from exc
            raise
        token = auth_store.create_session(user.id)
        set_session_cookie(response, token, settings)
        return AuthUserResponse(username=user.username)

    @router.post("/login", response_model=AuthUserResponse)
    def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
    ) -> AuthUserResponse:
        auth_store = request.app.state.auth_store
        if auth_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="계정 저장소를 사용할 수 없습니다.",
            )
        user = auth_store.authenticate(payload.username, payload.password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="아이디 또는 비밀번호가 올바르지 않습니다.",
            )
        token = auth_store.create_session(user.id)
        set_session_cookie(response, token, settings)
        return AuthUserResponse(username=user.username)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request, response: Response) -> Response:
        auth_store = request.app.state.auth_store
        if auth_store is not None:
            auth_store.revoke_session(request.cookies.get("monitube_session"))
        response.delete_cookie("monitube_session", path="/")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    return router
