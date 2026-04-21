from django.db.models import Count
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.permissions import IsGPUser, IsGPAdmin
from notifications.helpers import notify_org_admins
from .models import Fund, Scheme, Entity
from .serializers import (
    FundListSerializer, FundDetailSerializer, FundCreateSerializer,
    SchemeSerializer, EntitySerializer,
)


# ── Fund CRUD ──────────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def fund_list(request):
    """List all funds for the user's organization, or create a new fund."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        funds = (
            Fund.objects
            .filter(organization=org)
            .annotate(scheme_count=Count('schemes'))
        )
        return Response(FundListSerializer(funds, many=True).data)

    # POST — create
    if not request.user.is_admin:
        return Response({'detail': 'Only admins can create funds.'}, status=403)

    ser = FundCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    fund = ser.save(organization=org, created_by=request.user)

    log_audit(request, 'create', 'fund', fund.id, {'name': fund.name})
    notify_org_admins(
        org, 'New Fund Created',
        f'{fund.name} has been registered by {request.user.username}.',
        category='fund', resource_type='fund', resource_id=fund.id,
        created_by=request.user, exclude_user=request.user,
    )

    return Response(FundDetailSerializer(fund).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def fund_detail(request, fund_id):
    """Get, update, or delete a single fund."""
    org = request.organization
    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    if request.method == 'GET':
        return Response(FundDetailSerializer(fund).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can modify funds.'}, status=403)

    if request.method == 'PUT':
        ser = FundCreateSerializer(fund, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'fund', fund.id, {
            'name': fund.name, 'fields': list(request.data.keys()),
        })
        return Response(FundDetailSerializer(fund).data)

    # DELETE
    log_audit(request, 'delete', 'fund', fund.id, {'name': fund.name})
    fund.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Scheme CRUD ────────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def scheme_list(request, fund_id):
    """List schemes under a fund, or create a new scheme."""
    org = request.organization
    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    if request.method == 'GET':
        schemes = fund.schemes.all()
        return Response(SchemeSerializer(schemes, many=True).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can create schemes.'}, status=403)

    ser = SchemeSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    scheme = ser.save(fund=fund)
    log_audit(request, 'create', 'scheme', scheme.id, {
        'name': scheme.name, 'fund': str(fund.id),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def scheme_detail(request, scheme_id):
    """Get, update, or delete a scheme."""
    org = request.organization
    try:
        scheme = Scheme.objects.select_related('fund').get(
            pk=scheme_id, fund__organization=org,
        )
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if request.method == 'GET':
        return Response(SchemeSerializer(scheme).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can modify schemes.'}, status=403)

    if request.method == 'PUT':
        ser = SchemeSerializer(scheme, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'scheme', scheme.id, {
            'name': scheme.name, 'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'scheme', scheme.id, {'name': scheme.name})
    scheme.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Entity CRUD ────────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def entity_list(request, fund_id):
    """List entities for a fund, or add a new entity."""
    org = request.organization
    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    if request.method == 'GET':
        entities = fund.entities.all()
        return Response(EntitySerializer(entities, many=True).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can manage entities.'}, status=403)

    ser = EntitySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    entity = ser.save(fund=fund)
    log_audit(request, 'create', 'entity', entity.id, {
        'name': entity.name, 'role': entity.role, 'fund': str(fund.id),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['PUT', 'DELETE'])
@permission_classes([IsGPAdmin])
def entity_detail(request, entity_id):
    """Update or delete an entity."""
    org = request.organization
    try:
        entity = Entity.objects.select_related('fund').get(
            pk=entity_id, fund__organization=org,
        )
    except Entity.DoesNotExist:
        return Response({'detail': 'Entity not found.'}, status=404)

    if request.method == 'PUT':
        ser = EntitySerializer(entity, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'entity', entity.id, {
            'name': entity.name, 'role': entity.role,
            'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'entity', entity.id, {
        'name': entity.name, 'role': entity.role,
    })
    entity.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
