import mimetypes
from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.audit import log_audit, _get_client_ip
from accounts.permissions import IsGPUser, IsGPAdmin
from funds.models import Fund, Scheme
from .models import Document, DocumentAccessLog
from .serializers import (
    DocumentSerializer, DocumentUploadSerializer, DocumentAccessLogSerializer,
)


@api_view(['GET'])
@permission_classes([IsGPUser])
def document_list(request):
    """List documents for the user's organization, with optional filters."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    qs = Document.objects.filter(organization=org)

    # Filter by fund
    fund_id = request.query_params.get('fund')
    if fund_id:
        qs = qs.filter(fund_id=fund_id)

    # Filter by category
    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category=category)

    # Filter by visibility
    visibility = request.query_params.get('visibility')
    if visibility:
        qs = qs.filter(visibility=visibility)

    # Search by title
    search = request.query_params.get('search')
    if search:
        qs = qs.filter(title__icontains=search)

    return Response(DocumentSerializer(qs[:100], many=True).data)


@api_view(['POST'])
@permission_classes([IsGPAdmin])
@parser_classes([MultiPartParser, FormParser])
def document_upload(request):
    """Upload a new document to the vault."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    ser = DocumentUploadSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    uploaded_file = data['file']

    # Validate fund belongs to org
    fund = None
    if data.get('fund_id'):
        try:
            fund = Fund.objects.get(pk=data['fund_id'], organization=org)
        except Fund.DoesNotExist:
            return Response({'detail': 'Fund not found.'}, status=404)

    # Validate scheme belongs to fund
    scheme = None
    if data.get('scheme_id'):
        if not fund:
            return Response({'detail': 'Fund required when specifying scheme.'}, status=400)
        try:
            scheme = Scheme.objects.get(pk=data['scheme_id'], fund=fund)
        except Scheme.DoesNotExist:
            return Response({'detail': 'Scheme not found.'}, status=404)

    mime, _ = mimetypes.guess_type(uploaded_file.name)

    doc = Document.objects.create(
        organization=org,
        fund=fund,
        scheme=scheme,
        title=data['title'],
        description=data.get('description', ''),
        category=data.get('category', 'other'),
        visibility=data.get('visibility', 'internal'),
        file=uploaded_file,
        file_name=uploaded_file.name,
        file_size=uploaded_file.size,
        mime_type=mime or 'application/octet-stream',
        tags=data.get('tags', []),
        uploaded_by=request.user,
    )

    log_audit(request, 'create', 'document', doc.id, {
        'title': doc.title, 'category': doc.category, 'file_name': doc.file_name,
    })

    return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsGPUser])
def document_detail(request, doc_id):
    """Get document metadata."""
    org = request.organization
    try:
        doc = Document.objects.get(pk=doc_id, organization=org)
    except Document.DoesNotExist:
        return Response({'detail': 'Document not found.'}, status=404)

    # Log view access
    DocumentAccessLog.objects.create(
        document=doc,
        user=request.user,
        action='view',
        ip_address=_get_client_ip(request),
    )

    return Response(DocumentSerializer(doc).data)


@api_view(['GET'])
@permission_classes([IsGPUser])
def document_download(request, doc_id):
    """Download the document file."""
    org = request.organization
    try:
        doc = Document.objects.get(pk=doc_id, organization=org)
    except Document.DoesNotExist:
        return Response({'detail': 'Document not found.'}, status=404)

    # Log download
    DocumentAccessLog.objects.create(
        document=doc,
        user=request.user,
        action='download',
        ip_address=_get_client_ip(request),
    )

    log_audit(request, 'export', 'document', doc.id, {
        'title': doc.title, 'file_name': doc.file_name,
    })

    response = FileResponse(
        doc.file.open('rb'),
        content_type=doc.mime_type or 'application/octet-stream',
    )
    response['Content-Disposition'] = f'attachment; filename="{doc.file_name}"'
    return response


@api_view(['DELETE'])
@permission_classes([IsGPAdmin])
def document_delete(request, doc_id):
    """Delete a document."""
    org = request.organization
    try:
        doc = Document.objects.get(pk=doc_id, organization=org)
    except Document.DoesNotExist:
        return Response({'detail': 'Document not found.'}, status=404)

    log_audit(request, 'delete', 'document', doc.id, {
        'title': doc.title, 'file_name': doc.file_name,
    })

    # Delete actual file from storage
    doc.file.delete(save=False)
    doc.delete()

    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsGPAdmin])
def document_access_log(request, doc_id):
    """View who accessed/downloaded a document."""
    org = request.organization
    try:
        doc = Document.objects.get(pk=doc_id, organization=org)
    except Document.DoesNotExist:
        return Response({'detail': 'Document not found.'}, status=404)

    logs = doc.access_logs.all()[:50]
    return Response(DocumentAccessLogSerializer(logs, many=True).data)
