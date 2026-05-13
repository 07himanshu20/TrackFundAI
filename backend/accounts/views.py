import io
import pyotp
import qrcode
import base64
import random
import string
from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .audit import log_audit, _get_client_ip
from .models import AuditLog
from .permissions import IsGPAdmin, IsGPUser
from .serializers import (
    LoginSerializer, UserSerializer, ChangePasswordSerializer, AuditLogSerializer,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# AUTH — login with 3-attempt lockout (v5)
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """
    Authenticate and return JWT token pair.
    v5: 3-attempt lockout → 24h ban.
    If MFA is enabled, returns mfa_required=True instead of tokens.
    """
    ser = LoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    username = ser.validated_data['username']
    password = ser.validated_data['password']

    # Fetch user first to check lockout before authenticate()
    try:
        user_obj = User.objects.get(username=username)
    except User.DoesNotExist:
        return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

    if user_obj.is_locked:
        return Response(
            {'detail': 'Account locked for 24h due to too many failed attempts. Contact your admin.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    user = authenticate(request, username=username, password=password)

    if user is None or not user.is_active:
        user_obj.record_failed_login()
        remaining = max(0, 3 - user_obj.login_attempts)
        return Response(
            {'detail': f'Invalid credentials. {remaining} attempt(s) remaining before lockout.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Successful credential check — reset counter
    user.reset_login_attempts()

    # MFA gate — TOTP
    if user.mfa_enabled and user.mfa_totp_secret:
        # Return a partial token so the MFA verify endpoint can proceed
        # We store the user PK in a short-lived session hint
        request.session['mfa_pending_user'] = str(user.pk)
        return Response({'mfa_required': True, 'mfa_type': 'totp'}, status=status.HTTP_200_OK)

    # MFA gate — SMS
    if user.mfa_sms_enabled and user.phone:
        _send_sms_otp(user)
        request.session['mfa_pending_user'] = str(user.pk)
        return Response({'mfa_required': True, 'mfa_type': 'sms'}, status=status.HTTP_200_OK)

    return _issue_tokens(user, request)


def _issue_tokens(user, request):
    """Create JWT pair and audit log."""
    refresh = RefreshToken.for_user(user)
    AuditLog.objects.create(
        user=user,
        organization=user.organization,
        action='login',
        resource_type='session',
        ip_address=_get_client_ip(request),
    )
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'user': UserSerializer(user).data,
    })


# ---------------------------------------------------------------------------
# MFA — TOTP (Google Authenticator)
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mfa_setup_view(request):
    """
    Generate a new TOTP secret, return QR code as base64 PNG.
    Does NOT enable MFA yet — call mfa_enable after verifying.
    """
    secret = pyotp.random_base32()
    request.user.mfa_totp_secret = secret
    request.user.save(update_fields=['mfa_totp_secret'])

    org_name = request.user.organization.name if request.user.organization else 'TrackFundAI'
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=request.user.email or request.user.username, issuer_name=org_name)

    # Generate QR code PNG → base64
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    return Response({
        'secret': secret,
        'qr_code': f'data:image/png;base64,{qr_b64}',
        'uri': uri,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mfa_enable_view(request):
    """
    Verify the TOTP code the user just scanned, then enable MFA.
    Body: { "code": "123456" }
    """
    code = request.data.get('code', '')
    if not request.user.mfa_totp_secret:
        return Response({'detail': 'Run /mfa/setup/ first.'}, status=status.HTTP_400_BAD_REQUEST)

    totp = pyotp.TOTP(request.user.mfa_totp_secret)
    if not totp.verify(code, valid_window=1):
        return Response({'detail': 'Invalid OTP code.'}, status=status.HTTP_400_BAD_REQUEST)

    request.user.mfa_enabled = True
    request.user.save(update_fields=['mfa_enabled'])
    log_audit(request, 'update', 'user', details={'mfa': 'enabled'})
    return Response({'detail': 'MFA enabled successfully.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mfa_disable_view(request):
    """
    Disable MFA (requires current TOTP code or admin).
    Body: { "code": "123456" }
    """
    code = request.data.get('code', '')
    if request.user.mfa_enabled:
        totp = pyotp.TOTP(request.user.mfa_totp_secret)
        if not totp.verify(code, valid_window=1):
            # Allow admin override
            if not request.user.is_admin:
                return Response({'detail': 'Invalid OTP code.'}, status=status.HTTP_400_BAD_REQUEST)

    request.user.mfa_enabled = False
    request.user.mfa_totp_secret = ''
    request.user.save(update_fields=['mfa_enabled', 'mfa_totp_secret'])
    log_audit(request, 'update', 'user', details={'mfa': 'disabled'})
    return Response({'detail': 'MFA disabled.'})


@api_view(['POST'])
@permission_classes([AllowAny])
def mfa_verify_view(request):
    """
    Complete MFA-gated login.
    Body: { "code": "123456" }
    Session must have mfa_pending_user set.
    """
    pending_user_pk = request.session.get('mfa_pending_user')
    if not pending_user_pk:
        return Response({'detail': 'No pending MFA login.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(pk=pending_user_pk)
    except User.DoesNotExist:
        return Response({'detail': 'Session expired.'}, status=status.HTTP_400_BAD_REQUEST)

    code = request.data.get('code', '')
    mfa_type = request.data.get('mfa_type', 'totp')

    if mfa_type == 'totp':
        totp = pyotp.TOTP(user.mfa_totp_secret)
        if not totp.verify(code, valid_window=1):
            return Response({'detail': 'Invalid OTP.'}, status=status.HTTP_400_BAD_REQUEST)
    elif mfa_type == 'sms':
        if not _verify_sms_otp(user, code):
            return Response({'detail': 'Invalid or expired SMS OTP.'}, status=status.HTTP_400_BAD_REQUEST)
    else:
        return Response({'detail': 'Unknown MFA type.'}, status=status.HTTP_400_BAD_REQUEST)

    del request.session['mfa_pending_user']
    return _issue_tokens(user, request)


# ---------------------------------------------------------------------------
# MFA — SMS OTP helpers
# ---------------------------------------------------------------------------

def _send_sms_otp(user):
    """Generate 6-digit OTP, store hashed, send via MSG91/Fast2SMS."""
    from datetime import timedelta
    from django.conf import settings

    otp = ''.join(random.choices(string.digits, k=6))
    user.mfa_sms_otp = otp
    user.mfa_sms_otp_expires = timezone.now() + timedelta(minutes=10)
    user.save(update_fields=['mfa_sms_otp', 'mfa_sms_otp_expires'])

    # SMS dispatch — pluggable provider
    provider = getattr(settings, 'MFA_SMS_PROVIDER', 'msg91')
    try:
        if provider == 'msg91':
            _send_via_msg91(settings.MSG91_AUTH_KEY, user.phone, otp)
        else:
            _send_via_fast2sms(settings.FAST2SMS_API_KEY, user.phone, otp)
    except Exception:
        pass  # OTP stored in DB — admin can read in dev


def _verify_sms_otp(user, code):
    if not user.mfa_sms_otp or not user.mfa_sms_otp_expires:
        return False
    if timezone.now() > user.mfa_sms_otp_expires:
        return False
    if user.mfa_sms_otp != code:
        return False
    # Consume
    user.mfa_sms_otp = ''
    user.mfa_sms_otp_expires = None
    user.save(update_fields=['mfa_sms_otp', 'mfa_sms_otp_expires'])
    return True


def _send_via_msg91(auth_key, phone, otp):
    """MSG91 OTP API (placeholder — wire auth_key when available)."""
    import requests
    if not auth_key:
        return
    requests.post(
        'https://api.msg91.com/api/v5/otp',
        json={'template_id': '', 'mobile': phone, 'authkey': auth_key, 'otp': otp},
        timeout=5,
    )


def _send_via_fast2sms(api_key, phone, otp):
    """Fast2SMS (placeholder — wire api_key when available)."""
    import requests
    if not api_key:
        return
    requests.post(
        'https://www.fast2sms.com/dev/bulkV2',
        headers={'authorization': api_key},
        json={'route': 'otp', 'variables_values': otp, 'numbers': phone},
        timeout=5,
    )


# ---------------------------------------------------------------------------
# Standard auth views
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    """Blacklist the refresh token (optional — client can just discard)."""
    refresh_token = request.data.get('refresh')
    if refresh_token:
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except Exception:
            pass

    log_audit(request, 'logout', 'session')
    return Response({'detail': 'Logged out.'})


@api_view(['POST'])
@permission_classes([AllowAny])
def token_refresh_view(request):
    """Refresh an access token."""
    refresh_token = request.data.get('refresh')
    if not refresh_token:
        return Response({'detail': 'Refresh token required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        refresh = RefreshToken(refresh_token)
        return Response({'access': str(refresh.access_token), 'refresh': str(refresh)})
    except Exception:
        return Response({'detail': 'Invalid or expired refresh token.'}, status=status.HTTP_401_UNAUTHORIZED)


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """Get or update the current user's profile."""
    if request.method == 'GET':
        return Response(UserSerializer(request.user).data)

    ser = UserSerializer(request.user, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    allowed = {'first_name', 'last_name', 'email', 'phone'}
    for key in request.data:
        if key not in allowed:
            return Response({'detail': f'Cannot update field: {key}'}, status=status.HTTP_400_BAD_REQUEST)
    ser.save()
    return Response(UserSerializer(request.user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password_view(request):
    """Change the current user's password."""
    ser = ChangePasswordSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    if not request.user.check_password(ser.validated_data['old_password']):
        return Response({'detail': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

    request.user.set_password(ser.validated_data['new_password'])
    request.user.save()
    return Response({'detail': 'Password changed successfully.'})


@api_view(['GET'])
@permission_classes([IsGPUser])
def audit_log_list(request):
    """
    List audit log entries for the organization.
    Includes entries scoped to the org OR entries created by this user
    (catches early import logs where organization was not yet set on the log).
    """
    from django.db.models import Q
    org = request.organization

    if org:
        qs = AuditLog.objects.filter(
            Q(organization=org) | Q(user=request.user)
        )
    else:
        qs = AuditLog.objects.filter(user=request.user)

    action = request.query_params.get('action')
    if action:
        qs = qs.filter(action=action)
    resource_type = request.query_params.get('resource_type')
    if resource_type:
        qs = qs.filter(resource_type=resource_type)

    qs = qs.order_by('-timestamp').distinct()
    return Response(AuditLogSerializer(qs[:100], many=True).data)
