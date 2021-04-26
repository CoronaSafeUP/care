from django.db import transaction
from django.db.models import F
from django.db.models.query_utils import Q
from django_filters import Filter
from django_filters import rest_framework as filters
from drf_yasg.utils import swagger_auto_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from care.facility.api.serializers.patient_investigation import (
    InvestigationValueSerializer,
    PatientInvestigationGroupSerializer,
    PatientInvestigationSerializer,
    PatientInvestigationSessionSerializer,
)
from care.facility.models.patient_consultation import PatientConsultation
from care.facility.models.patient_investigation import (
    InvestigationSession,
    InvestigationValue,
    PatientInvestigation,
    PatientInvestigationGroup,
)
from care.users.models import User
from care.utils.cache.cache_allowed_facilities import get_accessible_facilities
from care.utils.cache.patient_investigation import get_investigation_id
from care.utils.filters import MultiSelectFilter


class InvestigationGroupFilter(filters.FilterSet):
    name = filters.CharFilter(field_name="name", lookup_expr="icontains")


class GroupFilter(Filter):
    def filter(self, qs, value):
        if not value:
            return qs

        qs = qs.filter(groups__external_id=value)
        return qs


class PatientInvestigationFilter(filters.FilterSet):
    name = filters.CharFilter(field_name="name", lookup_expr="icontains")
    group = GroupFilter()


class InvestigationGroupViewset(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PatientInvestigationGroupSerializer
    queryset = PatientInvestigationGroup.objects.all()
    lookup_field = "external_id"
    permission_classes = (IsAuthenticated,)
    filterset_class = InvestigationGroupFilter

    filter_backends = (filters.DjangoFilterBackend,)


class InvestigationResultsSetPagination(PageNumberPagination):
    page_size = 500


class PatientInvestigationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PatientInvestigationSerializer
    queryset = PatientInvestigation.objects.all()
    lookup_field = "external_id"
    permission_classes = (IsAuthenticated,)
    filterset_class = PatientInvestigationFilter
    filter_backends = (filters.DjangoFilterBackend,)
    pagination_class = InvestigationResultsSetPagination


class PatientInvestigationFilter(filters.FilterSet):
    created_date = filters.DateFromToRangeFilter(field_name="created_date")
    modified_date = filters.DateFromToRangeFilter(field_name="modified_date")
    investigation = filters.CharFilter(field_name="investigation__external_id")
    investigations = MultiSelectFilter(field_name="investigation__external_id")
    sessions = MultiSelectFilter(field_name="session__external_id")
    session = filters.CharFilter(field_name="session__external_id")


class InvestigationSummaryResultsSetPagination(PageNumberPagination):
    page_size = 500


class PatientInvestigationSummaryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = InvestigationValueSerializer
    queryset = InvestigationValue.objects.all()
    lookup_field = "external_id"
    permission_classes = (IsAuthenticated,)
    filterset_class = PatientInvestigationFilter
    filter_backends = (filters.DjangoFilterBackend,)
    pagination_class = InvestigationSummaryResultsSetPagination
    SESSION_PER_PAGE = 5

    def get_queryset(self):
        session_page = self.request.GET.get("session_page", 1)
        queryset = self.queryset.filter(consultation__patient__external_id=self.kwargs.get("patient_external_id"))
        sessions = queryset.order_by("session__created_date").distinct("session__created_date")[
            (session_page - 1) * self.SESSION_PER_PAGE : (session_page) * self.SESSION_PER_PAGE
        ]
        if not sessions.exists():
            return self.queryset.none()
        queryset = queryset.filter(session_id__in=sessions.values("session_id"))
        if self.request.user.is_superuser:
            return queryset
        elif self.request.user.user_type >= User.TYPE_VALUE_MAP["StateLabAdmin"]:
            return queryset.filter(consultation__patient__facility__state=self.request.user.state)
        elif self.request.user.user_type >= User.TYPE_VALUE_MAP["DistrictLabAdmin"]:
            return queryset.filter(consultation__patient__facility__district=self.request.user.district)
        allowed_facilities = get_accessible_facilities(self.request.user)
        filters = Q(consultation__patient__facility_id__in=allowed_facilities)
        filters |= Q(consultation__assigned_to=self.request.user)
        return queryset.filter(filters)


class InvestigationValueViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = InvestigationValueSerializer
    queryset = InvestigationValue.objects.all()
    lookup_field = "external_id"
    permission_classes = (IsAuthenticated,)
    filterset_class = PatientInvestigationFilter
    filter_backends = (filters.DjangoFilterBackend,)

    def get_queryset(self):
        queryset = self.queryset.filter(consultation__external_id=self.kwargs.get("consultation_external_id"))
        if self.request.user.is_superuser:
            return queryset
        elif self.request.user.user_type >= User.TYPE_VALUE_MAP["StateLabAdmin"]:
            return queryset.filter(consultation__patient__facility__state=self.request.user.state)
        elif self.request.user.user_type >= User.TYPE_VALUE_MAP["DistrictLabAdmin"]:
            return queryset.filter(consultation__patient__facility__district=self.request.user.district)
        filters = Q(consultation__patient__facility__users__id__exact=self.request.user.id)
        filters |= Q(consultation__assigned_to=self.request.user)
        return queryset.filter(filters).distinct("id")

    @swagger_auto_schema(responses={200: PatientInvestigationSessionSerializer(many=True)})
    @action(detail=False, methods=["GET"])
    def get_sessions(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        return Response(
            list(
                queryset.distinct("session")
                .annotate(
                    session_external_id=F("session__external_id"), session_created_date=F("session__created_date")
                )
                .values("session_external_id", "session_created_date")
            )
        )

    def create(self, request, *args, **kwargs):

        if "investigations" not in request.data:
            return Response({"investigation": "is required"}, status=status.HTTP_400_BAD_REQUEST)

        investigations = request.data["investigations"]

        if not isinstance(investigations, list):
            return Response({"error": "Data must be a list"}, status=status.HTTP_400_BAD_REQUEST)

        consultation = PatientConsultation.objects.get(external_id=kwargs.get("consultation_external_id"))
        consultation_id = consultation.id

        if consultation.discharge_date:
            return Response(
                {"consultation": ["Discharged Consultation data cannot be updated"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            session = InvestigationSession()
            session.save()

            for value in investigations:
                value["session"] = session.id
                value["investigation"] = get_investigation_id(value["investigation"])
                value["consultation"] = consultation_id

            serializer = self.get_serializer(data=investigations, many=True)
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)