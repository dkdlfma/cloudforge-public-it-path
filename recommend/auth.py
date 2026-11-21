"""Cognito 로그인 (Streamlit) — 담당: 김현진 / 사용자 풀: 팀장님(API Gateway + Cognito)
.env 에 COGNITO_CLIENT_ID 가 있으면 로그인 화면을 띄우고, 없으면 로그인 없이(로컬 시연) 진행합니다.
앱 클라이언트는 '클라이언트 시크릿 없음' + USER_PASSWORD_AUTH 허용으로 만들어야 합니다."""
import base64
import json
import os


def enabled():
    return bool(os.getenv("COGNITO_CLIENT_ID"))


def _claims(id_token):
    body = id_token.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


def login(username, password):
    """→ {"ok": True, "user": 사용자명, "sub": 고유ID, "id_token": ...} 또는 {"ok": False, "error": ...}"""
    import boto3
    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "ap-northeast-2")
    client = boto3.client("cognito-idp", region_name=region)
    try:
        r = client.initiate_auth(ClientId=os.getenv("COGNITO_CLIENT_ID"), AuthFlow="USER_PASSWORD_AUTH",
                                 AuthParameters={"USERNAME": username, "PASSWORD": password})
    except client.exceptions.NotAuthorizedException:
        return {"ok": False, "error": "아이디 또는 비밀번호가 맞지 않아요."}
    except client.exceptions.UserNotFoundException:
        return {"ok": False, "error": "등록되지 않은 사용자예요."}
    except Exception as e:
        return {"ok": False, "error": f"로그인 오류: {str(e)[:120]}"}
    if "AuthenticationResult" not in r:
        return {"ok": False, "error": f"추가 인증이 필요해요 ({r.get('ChallengeName')}). 관리자에게 문의하세요."}
    tok = r["AuthenticationResult"]["IdToken"]
    c = _claims(tok)
    return {"ok": True, "user": c.get("cognito:username") or username, "sub": c.get("sub"), "id_token": tok}


def gate(st):
    """Streamlit 화면 맨 위에서 호출 → 로그인한 사용자 이름(또는 None=로그인 기능 꺼짐) 반환"""
    if not enabled():
        return None
    if st.session_state.get("auth_user"):
        with st.sidebar:
            st.caption(f"👤 {st.session_state['auth_user']} 로그인됨")
            if st.button("로그아웃"):
                st.session_state.pop("auth_user", None)
                st.rerun()
        return st.session_state["auth_user"]
    st.subheader("🔐 로그인")
    u = st.text_input("아이디")
    p = st.text_input("비밀번호", type="password")
    if st.button("로그인"):
        r = login(u, p)
        if r["ok"]:
            st.session_state["auth_user"] = r["user"]
            st.rerun()
        st.error(r["error"])
    st.stop()
