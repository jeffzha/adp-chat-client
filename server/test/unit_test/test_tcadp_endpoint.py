from vendor.tcadp.tcadp import TCADP


def test_china_tencent_adp_uses_official_cloud_api_endpoint():
    vendor = TCADP(
        {
            "ServiceVendor": "ChinaTencentADP",
            "AppId": "app-1",
            "AppKey": "app-key",
            "SecretId": "secret-id",
            "SecretKey": "secret-key",
        },
        "app-1",
    )

    config = vendor.tc_config()

    assert config["adp"]["url"] == "https://adp.tencentcloudapi.com"
    assert config["adp"]["region"] == "ap-guangzhou"

