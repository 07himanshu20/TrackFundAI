from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .audit import log_audit, _get_client_ip
from .models import AuditLog
from .permissions import IsGPAdmin
from .serializers import (
    LoginSerializer, UserSerializer, ChangePasswordSerializer, AuditLogSerializer,
)


@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """Authenticate and return JWT token pair."""
    ser = LoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    user = authenticate(
        request,
        username=ser.validated_data['username'],
        password=ser.validated_data['password'],
    )
    if user is None or not user.is_active:
        return Response(
            {'detail': 'Invalid credentials.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

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
        return Response(
            {'detail': 'Refresh token required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        refresh = RefreshToken(refresh_token)
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        })
    except Exception:
        return Response(
            {'detail': 'Invalid or expired refresh token.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """Get or update the current user's profile."""
    if request.method == 'GET':
        return Response(UserSerializer(request.user).data)

    ser = UserSerializer(request.user, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    # Users can only update their own safe fields
    allowed = {'first_name', 'last_name', 'email', 'phone'}
    for key in request.data:
        if key not in allowed:
            return Response(
                {'detail': f'Cannot update field: {key}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    ser.save()
    return Response(UserSerializer(request.user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password_view(request):
    """Change the current user's password."""
    ser = ChangePasswordSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    if not request.user.check_password(ser.validated_data['old_password']):
        return Response(
            {'detail': 'Current password is incorrect.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    request.user.set_password(ser.validated_data['new_password'])
    request.user.save()

    return Response({'detail': 'Password changed successfully.'})


@api_view(['GET'])
@permission_classes([IsGPAdmin])
def audit_log_list(request):
    """List audit log entries for the organization (admin only)."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    qs = AuditLog.objects.filter(organization=org)

    # Filter by action
    action = request.query_params.get('action')
    if action:
        qs = qs.filter(action=action)

    # Filter by resource_type
    resource_type = request.query_params.get('resource_type')
    if resource_type:
        qs = qs.filter(resource_type=resource_type)

    # Filter by user
    user_id = request.query_params.get('user')
    if user_id:
        qs = qs.filter(user_id=user_id)

    return Response(AuditLogSerializer(qs[:100], many=True).data)
