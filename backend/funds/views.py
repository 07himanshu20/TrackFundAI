from django.db.models import Count
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import filter_funds_for_user, user_has_fund_access
from accounts.models import FundAccess
from accounts.permissions import IsGPUser, IsGPAdmin
from notifications.helpers import notify_org_admins
from .models import FundCategory, Entity, Fund, Scheme
from .serializers import (
    FundCategorySerializer,
    EntitySerializer, EntityListSerializer,
    FundListSerializer, FundDetailSerializer, FundCreateSerializer,
    SchemeSerializer,
)


# ── Fund Category CRUD ───────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def fund_category_list(request):
    """List all SEBI fund categories, or create a new one (admin only)."""
    if request.method == 'GET':
        categories = FundCategory.objects.all()
        return Response(FundCategorySerializer(categories, many=True).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can create fund categories.'}, status=403)

    ser = FundCategorySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    cat = ser.save()
    log_audit(request, 'create', 'fund_category', cat.id, {
        'code': cat.sebi_category_code, 'name': cat.name,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def fund_category_detail(request, category_id):
    """Get, update, or delete a fund category."""
    try:
        cat = FundCategory.objects.get(pk=category_id)
    except FundCategory.DoesNotExist:
        return Response({'detail': 'Fund category not found.'}, status=404)

    if request.method == 'GET':
        return Response(FundCategorySerializer(cat).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can modify fund categories.'}, status=403)

    if request.method == 'PUT':
        ser = FundCategorySerializer(cat, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'fund_category', cat.id, {
            'code': cat.sebi_category_code, 'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'fund_category', cat.id, {
        'code': cat.sebi_category_code,
    })
    cat.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Entity CRUD (organization-level) ────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def entity_list(request):
    """
    List all entities for the user's organization, or create a new entity.
    Entities are organization-level (shared across funds).
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        entities = Entity.objects.filter(organization=org)
        # Optional filter by entity_type
        entity_type = request.query_params.get('entity_type')
        if entity_type:
            entities = entities.filter(entity_type=entity_type)
        return Response(EntitySerializer(entities, many=True).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can manage entities.'}, status=403)

    ser = EntitySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    entity = ser.save(organization=org)
    log_audit(request, 'create', 'entity', entity.id, {
        'name': entity.entity_name, 'type': entity.entity_type,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def entity_detail(request, entity_id):
    """Get, update, or delete an entity."""
    org = request.organization
    try:
        entity = Entity.objects.get(pk=entity_id, organization=org)
    except Entity.DoesNotExist:
        return Response({'detail': 'Entity not found.'}, status=404)

    if request.method == 'GET':
        return Response(EntitySerializer(entity).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can modify entities.'}, status=403)

    if request.method == 'PUT':
        ser = EntitySerializer(entity, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'entity', entity.id, {
            'name': entity.entity_name, 'type': entity.entity_type,
            'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'entity', entity.id, {
        'name': entity.entity_name, 'type': entity.entity_type,
    })
    entity.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


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
            .select_related('fund_category')
            .annotate(scheme_count=Count('schemes'))
        )
        funds = filter_funds_for_user(funds, request.user)
        return Response(FundListSerializer(funds, many=True).data)

    # POST — create
    if not request.user.is_admin:
        return Response({'detail': 'Only admins can create funds.'}, status=403)

    ser = FundCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    fund = ser.save(organization=org, created_by=request.user)

    # Auto-grant fund access to the creator
    FundAccess.objects.get_or_create(
        user=request.user, fund=fund,
        defaults={'access_level': 'admin'},
    )

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
        fund = Fund.objects.select_related(
            'fund_category',
            'manager_entity', 'trustee_entity', 'sponsor_entity',
            'custodian_entity', 'auditor_entity',
        ).get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    if not user_has_fund_access(request.user, fund):
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

    if not user_has_fund_access(request.user, fund):
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

    if not user_has_fund_access(request.user, scheme.fund):
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
