# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Sandbox manager for orchestrating multiple Docker-based sandbox instances.

This module provides management functionality for multiple sandbox instances,
including lifecycle management, status monitoring, and resource cleanup for
secure command execution environments.
"""
import uuid
from typing import Dict

import docker
from ..sandbox.sandbox import Sandbox, SandboxStatus
from pobi_v2.core.config import settings


class SandboxManager:
    """Manages multiple Docker sandbox instances for secure command execution.
    
    The SandboxManager provides a centralized way to create, manage, and control
    multiple Docker-based sandbox containers. It maintains a registry of active
    sandboxes and provides methods for lifecycle management and command execution.
    
    Attributes:
        docker_client: Docker client instance for container operations
        sandboxes: Dictionary mapping sandbox IDs to Sandbox instances
        
    Example:
        >>> manager = SandboxManager()
        >>> sandbox_id = manager.create_sandbox("ubuntu:latest", network_name="host")
        >>> sandbox = manager.get_sandbox(sandbox_id)
        >>> result = manager.execute_in_sandbox(sandbox_id, "ls -la")
        >>> manager.stop_all()
    """
    def __init__(self):
        """Initialize the SandboxManager with Docker client and empty sandbox registry.

        Creates a new manager instance with a Docker client configured for
        container operations and initializes an empty sandbox registry.
        """
        self.docker_client = docker.from_env()
        # key：sandbox.container_id（Docker 容器 ID 字符串，attach 后或 start 后赋值）
        self.sandboxes: Dict[str, Sandbox] = {}

    def create_sandbox(
            self,
            image: str | None = None,
            volume_path: str | None = None,
            network_name: str | None = None
        ):
        """Create a new Docker sandbox instance.
        
        Creates and starts a new sandbox container with the specified configuration.
        The sandbox is automatically registered in the manager's sandbox registry.
        
        Args:
            image: Docker image name/tag to use for the container (default: "ubuntu:latest")
            volume_path: Optional path to mount as read-only volume at `/challenge`
            network_name: Docker network name to connect the container to (default: "host")

        Returns:
            uuid.UUID: Unique identifier for the created sandbox
            
        Raises:
            docker.errors.ImageNotFound: If the specified Docker image doesn't exist
            docker.errors.DockerException: If container creation fails
            
        Note:
            The sandbox is automatically started upon creation and ready for command execution.
        """
        sandbox_id = uuid.uuid4()

        if image is None:
            image = settings.sandbox_image
        if network_name is None:
            network_name = settings.sandbox_network

        new_sb = Sandbox(
            docker_client=self.docker_client,
            container_id=str(sandbox_id)
        )
        new_sb.start(container_image=image, volume_path=volume_path, network_name=network_name)

        self.sandboxes[new_sb.container_id] = new_sb
        return new_sb.container_id

    def get_sandbox(self, sandbox_id: uuid.UUID) -> Sandbox | None:
        """Retrieve a sandbox instance by its unique identifier.
        
        Args:
            sandbox_id: Unique identifier of the sandbox to retrieve
            
        Returns:
            Sandbox instance if found, None otherwise
        """
        return self.sandboxes.get(sandbox_id)

    def get_or_create_shared_kali(self) -> Sandbox:
        """获取全局共享的 Kali 沙箱单例（常驻容器）。

        全面容器化后，Kali 作为 docker-compose 的常驻 service 由宿主机 daemon 创建，
        所有 shell 命令与 Python 验证共用同一容器。本方法按容器名解析：
        - 若容器已存在则复用（attach_existing），不重复创建；
        - 若不存在（如本地非 compose 运行）则创建并接入统一网络 pobi_net。

        Returns:
            Sandbox: 指向全局共享 Kali 容器的沙箱实例。

        Raises:
            docker.errors.DockerException: 若容器创建或连接失败。
        """
        container_name = settings.kali_container_name
        sandbox = Sandbox(docker_client=self.docker_client, container_id=str(uuid.uuid4()))

        try:
            # 复用 compose 中已存在的常驻 kali 容器
            sandbox.attach_existing(container_name, container_image=settings.sandbox_image)
            self.sandboxes[sandbox.container_id] = sandbox
            return sandbox
        except docker.errors.NotFound:
            pass

        # 本地非 compose 场景：创建常驻 kali 容器并接入统一网络
        image = self.docker_client.images.get(settings.sandbox_image)
        container = self.docker_client.containers.run(
            image=image,
            name=container_name,
            network=settings.sandbox_network,
            extra_hosts={"host.docker.internal": "host-gateway"},
            tty=True,
            command="/bin/bash",
            detach=True,
            stdin_open=True,
        )
        sandbox.container_id = container.id
        sandbox.docker_image = settings.sandbox_image
        sandbox.status = SandboxStatus.RUNNING
        self.sandboxes[sandbox.container_id] = sandbox
        return sandbox

    def execute_in_sandbox(self, sandbox_id: uuid.UUID, command: str):
        """Execute a command in the specified sandbox.
        
        Runs the command in the identified sandbox and returns the execution results.
        The command execution includes stdout, stderr, and exit code information.
        
        Args:
            sandbox_id: Unique identifier of the target sandbox
            command: Command string to execute within the sandbox
            
        Returns:
            dict: Command execution results including stdout, stderr, exit_code, etc.
            
        Raises:
            ValueError: If sandbox not found or not in running status
            Exception: If command execution fails
            
        Example:
            >>> result = manager.execute_in_sandbox(sandbox_id, "ls -la")
            >>> print(result['stdout'])
        """
        if self.sandboxes[sandbox_id] != None and self.sandboxes[sandbox_id].status == SandboxStatus.RUNNING:
            try: 
                sandbox = self.sandboxes[sandbox_id]
                output = sandbox.execute_command(command=command)
                return output
            except Exception as e: 
                raise e 
        else: 
            raise ValueError(f"Sandbox with ID {sandbox_id} not found or status not running.")
    
    def stop_all(self):
        """Stop all managed sandbox instances.
        
        Gracefully stops all containers managed by this SandboxManager.
        This method is useful for cleanup operations and shutdown procedures.
        
        Note:
            Stopped containers remain in memory but are no longer consuming resources.
            Use individual cleanup methods on sandbox instances for complete removal.
        """
        for sandbox_id in self.sandboxes.keys():
            self.sandboxes[sandbox_id].stop()

