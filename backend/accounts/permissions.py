from rest_framework.permissions import BasePermission


class IsGPAdmin(BasePermission):
    """Only platform_admin or gp_admin can access."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_admin


class IsGPUser(BasePermission):
    """Any GP-side user (admin, user, compliance, accountant)."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_gp


class IsGPOrReadOnly(BasePermission):
    """GP users get full access; others get read-only."""
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        return request.user.is_gp
