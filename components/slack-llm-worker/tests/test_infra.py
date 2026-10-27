import json
from pathlib import Path


def test_review_template_is_fail_closed_and_scoped():
    template = json.loads((Path(__file__).parents[1] / "infra/cloudformation.json").read_text())
    resources = template["Resources"]
    assert not any(r["Type"] in {"AWS::ECS::Service", "AWS::EC2::NatGateway", "AWS::EKS::Cluster"}
                   for r in resources.values())
    env = resources["WorkerTask"]["Properties"]["ContainerDefinitions"][0]["Environment"]
    assert {x["Name"]: x["Value"] for x in env}["ENABLE_PAID_MODEL"] == "disabled"
    statements = resources["WorkerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert all(s["Resource"] != "*" and "*" not in s["Action"] for s in statements)
    queue, dlq = [resources[n]["Properties"] for n in ["Queue", "DLQ"]]
    assert queue["SqsManagedSseEnabled"] and dlq["SqsManagedSseEnabled"]
    assert dlq["MessageRetentionPeriod"] > queue["MessageRetentionPeriod"]
    assert queue["RedrivePolicy"]["maxReceiveCount"] >= 3


def test_template_refs_exist():
    template = json.loads((Path(__file__).parents[1] / "infra/cloudformation.json").read_text())
    names = set(template["Resources"]) | set(template["Parameters"])
    def visit(value):
        if isinstance(value, dict):
            if "Ref" in value:
                assert value["Ref"] in names or value["Ref"].startswith("AWS::")
            if "Fn::GetAtt" in value:
                assert value["Fn::GetAtt"][0] in template["Resources"]
            for v in value.values(): visit(v)
        elif isinstance(value, list):
            for v in value: visit(v)
    visit(template)
