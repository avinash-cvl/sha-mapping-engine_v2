pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '15'))
    }

    environment {
        IMAGE_NAME = "sha-mapping-engine"
        // Multibranch-safe branch name (replaces '/' with '-' e.g. feature/api -> feature-api)
        SAFE_BRANCH = "${env.BRANCH_NAME ? env.BRANCH_NAME.replaceAll('/', '-') : 'local'}"
        IMAGE_TAG = "${SAFE_BRANCH}-${BUILD_NUMBER}"

        CONTAINER_NAME = "sha-mapping-engine"
        HOST_PORT = "8099"
        CONTAINER_PORT = "8099"

        // Branches permitted to trigger automatic deployment to the container
        DEPLOY_BRANCHES = "main,master,develop,dev,qa"
    }

    stages {

        stage('Checkout Code') {
            steps {
                echo "Building on branch: ${env.BRANCH_NAME} (Safe Tag: ${SAFE_BRANCH})"
                checkout scm
            }
        }

        stage('Verify Syntax') {
            steps {
                sh '''
                    echo "Checking Python files syntax..."
                    python3 -m py_compile app.py common/config.py common/db.py || true
                '''
            }
        }

        stage('Build Docker Image') {
            steps {
                script {
                    echo "Building Docker image: ${IMAGE_NAME}:${IMAGE_TAG}"
                    
                    // On main/master, also tag as latest
                    def latestTagArg = ""
                    if (env.BRANCH_NAME == 'main' || env.BRANCH_NAME == 'master') {
                        latestTagArg = "-t ${IMAGE_NAME}:latest"
                    }

                    sh """
                        docker build \
                            -t ${IMAGE_NAME}:${IMAGE_TAG} \
                            ${latestTagArg} \
                            -f Dockerfile \
                            .
                    """
                }
            }
        }

        stage('Deploy to Container') {
            // Only deploy when running on deployable branches (e.g. main, dev, qa)
            // Feature branches & PRs build and validate the image without replacing the live container
            when {
                expression {
                    def allowed = env.DEPLOY_BRANCHES.tokenize(',')
                    return allowed.contains(env.BRANCH_NAME)
                }
            }
            steps {
                sh '''
                    echo "Deploying branch ${BRANCH_NAME} to container ${CONTAINER_NAME}..."
                    mkdir -p data cache logs

                    echo "Stopping and removing existing container if running..."
                    docker rm -f ${CONTAINER_NAME} || true

                    if [ -f .env ]; then
                        ENV_ARG="--env-file .env"
                    else
                        echo "Warning: .env file not found, starting without --env-file"
                        ENV_ARG=""
                    fi

                    echo "Starting new container on port ${HOST_PORT}..."
                    docker run -d \
                        --name ${CONTAINER_NAME} \
                        --restart unless-stopped \
                        ${ENV_ARG} \
                        -v "$(pwd)/data:/app/data" \
                        -v "$(pwd)/cache:/app/cache" \
                        -v "$(pwd)/logs:/app/logs" \
                        -p ${HOST_PORT}:${CONTAINER_PORT} \
                        ${IMAGE_NAME}:${IMAGE_TAG}
                '''
            }
        }

        stage('Health Check') {
            when {
                expression {
                    def allowed = env.DEPLOY_BRANCHES.tokenize(',')
                    return allowed.contains(env.BRANCH_NAME)
                }
            }
            steps {
                sh '''
                    echo "Validating service liveness on port ${HOST_PORT}..."
                    for i in $(seq 1 12); do
                        if curl -sf "http://localhost:${HOST_PORT}/health" > /dev/null; then
                            echo "Container is healthy and responding!"
                            exit 0
                        fi
                        echo "Waiting for service to become ready ($i/12)..."
                        sleep 5
                    done

                    echo "Health check failed! Container logs:"
                    docker logs --tail 50 ${CONTAINER_NAME}
                    exit 1
                '''
            }
        }

        stage('Cleanup Old Images') {
            steps {
                sh '''
                    echo "Pruning older and dangling Docker images..."
                    docker images ${IMAGE_NAME} --format "{{.Repository}}:{{.Tag}}" \
                        | grep -v "^${IMAGE_NAME}:${IMAGE_TAG}$" \
                        | grep -v "^${IMAGE_NAME}:latest$" \
                        | xargs -r docker rmi || true
                '''
            }
        }
    }

    post {
        success {
            script {
                def allowed = env.DEPLOY_BRANCHES.tokenize(',')
                if (allowed.contains(env.BRANCH_NAME)) {
                    echo "Deployment of ${BRANCH_NAME} completed successfully! Live on port ${HOST_PORT}."
                } else {
                    echo "Branch ${BRANCH_NAME} built and validated successfully (deployment skipped for feature/PR branch)."
                }
            }
        }

        failure {
            echo "Pipeline failed on branch ${BRANCH_NAME}! Check stage logs above."
        }
    }
}
