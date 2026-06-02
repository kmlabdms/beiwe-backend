import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    TimeZone,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_scheduler as scheduler,
    aws_scheduler_targets as scheduler_targets,
)
from constructs import Construct


# Pause logic — mirrors manage_beiwe.py do_pause() but saves state to SSM
# instead of a local file so manage_beiwe.py resume can read it back.
_PAUSE_CODE = """\
import boto3, json, os

def handler(event, context):
    region   = os.environ['REGION']
    env_name = os.environ['ENV_NAME']

    eb  = boto3.client('elasticbeanstalk', region_name=region)
    asg = boto3.client('autoscaling',      region_name=region)
    ec2 = boto3.client('ec2',              region_name=region)
    rds = boto3.client('rds',              region_name=region)
    ssm = boto3.client('ssm',              region_name=region)

    # --- EB ASG → 0 ---------------------------------------------------------
    resources = eb.describe_environment_resources(EnvironmentName=env_name)
    asg_name  = resources['EnvironmentResources']['AutoScalingGroups'][0]['Name']

    groups = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    g      = groups['AutoScalingGroups'][0]
    state  = {'asg_name': asg_name, 'MinSize': g['MinSize'],
               'MaxSize': g['MaxSize'], 'DesiredCapacity': g['DesiredCapacity']}

    ssm.put_parameter(
        Name=f'/beiwe/{env_name}/suspend_state',
        Value=json.dumps(state),
        Type='String',
        Overwrite=True,
    )

    asg.update_auto_scaling_group(
        AutoScalingGroupName=asg_name, MinSize=0, MaxSize=0, DesiredCapacity=0
    )
    print(f'ASG {asg_name} set to 0')

    # --- Processing servers → stop ------------------------------------------
    reservations = ec2.describe_instances(
        Filters=[
            {'Name': 'tag:Name', 'Values': [
                f'{env_name} data processing manager',
                f'{env_name} data processing server',
            ]},
            {'Name': 'instance-state-name', 'Values': ['running']},
        ]
    )['Reservations']
    ids = [i['InstanceId'] for r in reservations for i in r['Instances']]
    if ids:
        ec2.stop_instances(InstanceIds=ids)
        print(f'Stopping instances: {ids}')
    else:
        print('No running processing servers')

    # --- RDS → stop ---------------------------------------------------------
    db_id = f'{env_name}-database'
    try:
        status = rds.describe_db_instances(
            DBInstanceIdentifier=db_id)['DBInstances'][0]['DBInstanceStatus']
        if status == 'available':
            rds.stop_db_instance(DBInstanceIdentifier=db_id)
            print(f'RDS {db_id} stop requested')
        else:
            print(f'RDS {db_id} is {status!r}, skipping')
    except Exception as exc:
        print(f'RDS error (may not exist yet): {exc}')

    return {'status': 'paused', 'asg': asg_name, 'instances': ids, 'rds': db_id}
"""


class BeiweSchedulerStack(Stack):
    """
    Runs the Beiwe pause routine daily at 7 pm CST (01:00 UTC).

    Note on DST: 01:00 UTC = 7 pm CST (UTC-6, Nov-Mar) / 8 pm CDT (UTC-5, Mar-Nov).
    To target a consistent local time through DST, deploy two EventBridge rules
    Uses EventBridge Scheduler (not the older EventBridge Rules) so the cron
    expression runs in America/Chicago time and automatically adjusts for DST —
    no manual rule-toggling needed at clock changes.

    ASG state is saved to SSM Parameter Store at /beiwe/{env}/suspend_state so
    that `manage_beiwe.py resume` can read it back even when the Lambda ran the pause.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        env_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        pause_fn = lambda_.Function(
            self,
            "PauseFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(60),
            code=lambda_.Code.from_inline(_PAUSE_CODE),
            environment={
                "REGION":   self.region,
                "ENV_NAME": env_name,
            },
        )

        pause_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "elasticbeanstalk:DescribeEnvironmentResources",
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:UpdateAutoScalingGroup",
                    "ec2:DescribeInstances",
                    "ec2:StopInstances",
                    "rds:DescribeDBInstances",
                    "rds:StopDBInstance",
                    "ssm:PutParameter",
                ],
                resources=["*"],
            )
        )

        # EventBridge Scheduler with America/Chicago timezone — fires at exactly
        # 7 pm Central every day and handles DST automatically.
        schedule = scheduler.Schedule(
            self,
            "DailyPause",
            schedule=scheduler.ScheduleExpression.cron(
                hour="19",
                minute="0",
                time_zone=TimeZone.AMERICA_CHICAGO,
            ),
            target=scheduler_targets.LambdaInvoke(
                pause_fn,
                retry_attempts=1,
            ),
            description=f"Pause Beiwe cost drivers for {env_name} daily at 7 pm Central",
        )

        CfnOutput(self, "PauseFunctionArn", value=pause_fn.function_arn)
        CfnOutput(self, "ScheduleArn",      value=schedule.schedule_arn)
