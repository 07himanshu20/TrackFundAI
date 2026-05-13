from rest_framework import serializers
from .models import DealPipeline, ICPresentation, ICVote, ICDecision


class DealPipelineSerializer(serializers.ModelSerializer):
    sourced_by_name = serializers.CharField(source='sourced_by.get_full_name', read_only=True)
    stage_display = serializers.CharField(source='get_stage_display', read_only=True)

    class Meta:
        model = DealPipeline
        fields = '__all__'
        read_only_fields = ('id', 'organization', 'created_at', 'updated_at', 'sourced_by')


class ICVoteSerializer(serializers.ModelSerializer):
    voter_name = serializers.CharField(source='voter.get_full_name', read_only=True)
    vote_display = serializers.CharField(source='get_vote_display', read_only=True)

    class Meta:
        model = ICVote
        fields = '__all__'
        read_only_fields = ('id', 'voter', 'voted_at')


class ICPresentationSerializer(serializers.ModelSerializer):
    votes = ICVoteSerializer(many=True, read_only=True)
    outcome_display = serializers.CharField(source='get_outcome_display', read_only=True)

    class Meta:
        model = ICPresentation
        fields = '__all__'
        read_only_fields = ('id', 'deal', 'presenter', 'created_at')


class ICDecisionSerializer(serializers.ModelSerializer):
    decided_by_name = serializers.CharField(source='decided_by.get_full_name', read_only=True)

    class Meta:
        model = ICDecision
        fields = '__all__'
        read_only_fields = ('id', 'presentation', 'decided_by', 'created_at')
