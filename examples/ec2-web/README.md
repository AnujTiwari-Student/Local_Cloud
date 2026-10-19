# EC2 web app

From the repository root, build with `docker build -t localcloud-ec2-web:dev examples/ec2-web`.

Follow [the complete launch, token login, port publishing and cleanup walkthrough](../../docs/EC2.md#deploy-a-web-application).

This dependency-free Python app listens on `0.0.0.0:8080` inside its container. Launch with `--publish 8088:8080` to visit it at `http://127.0.0.1:8088` on the Docker host. The LocalCloud API port (for example, 4567) is separate from the app port (8088).
