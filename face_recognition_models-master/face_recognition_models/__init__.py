# -*- coding: utf-8 -*-

__author__ = """Adam Geitgey"""
__email__ = 'ageitgey@gmail.com'
__version__ = '0.1.0'

import os
import sys
from pathlib import Path

def _get_model_path(model_name):
    """Get the path to a model file."""
    # Try to find the models directory relative to this file
    current_dir = Path(__file__).parent
    models_dir = current_dir / "models"
    
    model_path = models_dir / model_name
    
    if model_path.exists():
        return str(model_path)
    
    # Fallback: try to find in site-packages
    try:
        import pkg_resources
        return pkg_resources.resource_filename(__name__, f"models/{model_name}")
    except (ImportError, Exception):
        # Last resort: return the path we tried
        return str(model_path)

def pose_predictor_model_location():
    return _get_model_path("shape_predictor_68_face_landmarks.dat")

def pose_predictor_five_point_model_location():
    return _get_model_path("shape_predictor_5_face_landmarks.dat")

def face_recognition_model_location():
    return _get_model_path("dlib_face_recognition_resnet_model_v1.dat")

def cnn_face_detector_model_location():
    return _get_model_path("mmod_human_face_detector.dat")

