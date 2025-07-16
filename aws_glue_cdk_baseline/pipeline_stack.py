# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from aws_cdk.pipelines import CodePipelineSource
from aws_cdk.pipelines import CodePipeline, ShellStep
from aws_cdk.aws_codebuild import LinuxBuildImage, BuildEnvironment, ComputeType
from aws_cdk import SecretValue
from typing import Dict
from aws_cdk import (
    Environment,
    Stack,
    aws_codecommit as codecommit,
    aws_iam as iam
)
from constructs import Construct
from aws_cdk.pipelines import CodePipeline, CodePipelineSource, CodeBuildStep, ManualApprovalStep, ShellStep
from helper import create_archive
from aws_glue_cdk_baseline.glue_app_stage import GlueAppStage


class PipelineStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, config: Dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        archive_file = create_archive()

        codecommit_repo = codecommit.Repository(
            self, "GitRepo",
            repository_name="aws-glue-cdk-baseline",
            code=codecommit.Code.from_zip_file(
                file_path=archive_file,
                branch="main"
            )
        )

        i_codecommit_repo = codecommit.Repository.from_repository_name(
            self, "IGlueRepo",
            codecommit_repo.repository_name
        )

        source = CodePipelineSource.git_hub( "gopi1949/aws-glue-cdk-baseline","main",
                                            authentication=SecretValue.secrets_manager("github-token")
                                            ) # fallback to polling mode
        
        pipeline = CodePipeline(self, "GluePipeline",
            pipeline_name="GluePipeline",
            cross_account_keys=True,
            docker_enabled_for_synth=True,
            synth=CodeBuildStep("CdkSynth_UnitTest",
                input=source,
                install_commands=[
                    # Create and activate virtual environment
                    "python -m venv .venv",
                    "source .venv/bin/activate",
                    # Install dependencies
                    "pip install -r requirements-dev.txt",
                    "pip install -r requirements.txt",
                    # Install CDK globally
                    "npm install -g aws-cdk",
                ], 
                commands=[
                    # Activate virtual environment for all commands
                    "source .venv/bin/activate",
                    
                    # Run CDK synthesis
                    "cdk synth -c stage=dev",
                    
                    # Run CDK unit tests
                    "python -m pytest tests/unit/ -v",
                    
                    # Set up Glue job testing environment
                    "export WORKSPACE_LOCATION=$(pwd)/aws_glue_cdk_baseline/job_scripts/",
                    "echo 'Workspace location: $WORKSPACE_LOCATION'",
                    
                    # Pull Glue Docker image
                    "docker pull amazon/aws-glue-libs:glue_libs_4.0.0_image_01",
                    
                    # Run Glue job tests in Docker container
                    "docker run --rm " +
                    "-v ~/.aws:/home/glue_user/.aws " +
                    "-v $WORKSPACE_LOCATION:/home/glue_user/workspace/ " +
                    "-e DISABLE_SSL=true " +
                    f"-e AWS_REGION={config['pipelineAccount']['awsRegion']} " +
                    "-e AWS_CONTAINER_CREDENTIALS_RELATIVE_URI " +
                    "--name glue_pytest " +
                    "amazon/aws-glue-libs:glue_libs_4.0.0_image_01 " +
                    "-c \"cd /home/glue_user/workspace && python3 -m pytest -v\"",
                ],
                build_environment=BuildEnvironment(
                    build_image=LinuxBuildImage.STANDARD_7_0,
                    privileged=True,  # Required for Docker-in-Docker
                    compute_type=ComputeType.MEDIUM,  # More resources for Docker operations
                ),
                role_policy_statements=[
                    # S3 read only
                    iam.PolicyStatement(
                        actions=[
                            "s3:ListBucket",
                            "s3:GetObject"
                        ],
                        resources=["*"]
                    ),
                    # Glue read only
                    iam.PolicyStatement(
                        actions=[
                            "glue:GetDatabase",
                            "glue:GetDatabases",
                            "glue:GetTable",
                            "glue:GetTables",
                            "glue:GetTableVersion",
                            "glue:GetTableVersions",
                            "glue:GetPartition",
                            "glue:GetPartitions",
                            "glue:BatchGetPartition",
                            "glue:GetPartitionIndexes",
                        ],
                        resources=[
                            "arn:aws:glue:*:*:catalog",
                            "arn:aws:glue:*:*:database/*",
                            "arn:aws:glue:*:*:table/*"
                        ]
                    ),
                    # Docker and ECR permissions
                    iam.PolicyStatement(
                        actions=[
                            "ecr:GetAuthorizationToken",
                            "ecr:BatchCheckLayerAvailability",
                            "ecr:GetDownloadUrlForLayer",
                            "ecr:BatchGetImage",
                        ],
                        resources=["*"]
                    ),
                    # STS permissions for cross-account access
                    iam.PolicyStatement(
                        actions=[
                            "sts:AssumeRole",
                            "sts:GetCallerIdentity",
                        ],
                        resources=["*"]
                    )
                ],
                 
            )
        )
        
        # Dev deployment
        dev_stage_name = "DeployDev"
        if config["pipelineAccount"]["awsAccountId"] == config["devAccount"]["awsAccountId"] and config["pipelineAccount"]["awsRegion"] == config["devAccount"]["awsRegion"]:
            dev_env = None
        else:
            dev_env = Environment(
                account=str(config["devAccount"]["awsAccountId"]), 
                region=config["devAccount"]["awsRegion"]
            )
        dev_stage_app = GlueAppStage(
            self, 
            dev_stage_name,
            config=config,
            stage="dev",
            env=dev_env
        )
        dev_stage = pipeline.add_stage(dev_stage_app)


        # Integ test
        dev_stage.add_post(CodeBuildStep("IntegrationTest",
                input=source,
                install_commands=[
                    "python -m venv .venv",
                    "source .venv/bin/activate",
                    "pip install -r requirements-dev.txt"
                ],
                commands=[
                    "source .venv/bin/activate",
                    # Integ test for Glue App stack
                    f"python $(pwd)/tests/integ/integ_test_glue_app_stack.py --account {str(config['devAccount']['awsAccountId'])} --region {config['devAccount']['awsRegion']} --stage-name {dev_stage_name} --sts-role-arn {dev_stage_app.iam_role_arn}",
                ],
                build_environment=BuildEnvironment(
                    build_image=LinuxBuildImage.STANDARD_7_0,
                    compute_type=ComputeType.SMALL,
                ),
                role_policy_statements=[
                    # STS for cross-account access
                    iam.PolicyStatement(
                        actions=[
                            "sts:AssumeRole"
                        ],
                        resources=[
                            "*"
                        ]
                    )
                ]
            )
        )

        # Prod deployment
        prod_stage_name = "DeployProd"
        if config["pipelineAccount"]["awsAccountId"] == config["prodAccount"]["awsAccountId"] and config["pipelineAccount"]["awsRegion"] == config["prodAccount"]["awsRegion"]:
            prod_env = None
        else:
            prod_env = Environment(
                account=str(config["prodAccount"]["awsAccountId"]), 
                region=config["prodAccount"]["awsRegion"]
            )
        prod_stage_app = GlueAppStage(
            self, 
            prod_stage_name,
            config=config,
            stage="prod",
            env=prod_env
        )
        prod_stage = pipeline.add_stage(prod_stage_app)
        prod_stage.add_pre(ManualApprovalStep("Approval"))
        