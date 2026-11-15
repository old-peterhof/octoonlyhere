from setuptools import setup

setup(
    name="OctoPrint-PrintPocket",
    version="0.1.0",
    description="A phone-first web app and push notifications for OctoPrint. Self hosted, no cloud account.",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    license="AGPLv3",
    packages=["octoprint_printpocket"],
    include_package_data=True,
    package_data={
        "octoprint_printpocket": [
            "templates/*.jinja2",
            "static/app/*",
            "static/app/icons/*",
        ]
    },
    zip_safe=False,
    python_requires=">=3.7,<4",
    install_requires=["pywebpush>=1.14"],
    entry_points={"octoprint.plugin": ["printpocket = octoprint_printpocket"]},
)
